import datetime as dt
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Account, AllocationTarget, Balance, Household, MonthlyExpense, Person, Snapshot

from . import data, engine, services


def inputs(**kw):
    base = dict(as_of=dt.date(2026, 11, 1), investments={"equity": 6e7}, bank_cash=5e6,
                monthly_expenses=2e5, expenses_include_emi=True, loan_outstanding=5e6, emi=6e4,
                loan_rate=0.085, monthly_investment=1e5, swr=0.0333, inflation=0.06, emergency_months=12)
    base.update(kw)
    return engine.FiInputs(**base)


class EngineTests(TestCase):
    def test_derived_numbers(self):
        i = inputs()
        self.assertEqual(i.core_expenses, 1.4e5)            # EMI removed
        self.assertEqual(i.emergency_fund, 2.4e6)
        self.assertEqual(i.excess_cash, 2.6e6)
        self.assertEqual(i.corpus, 6.26e7)
        self.assertAlmostEqual(i.fi_target, 1.4e5 * 12 / 0.0333 + 5e6)
        self.assertAlmostEqual(i.safe_monthly_income, (6.26e7 - 5e6) * 0.0333 / 12)

    def test_emi_not_in_expenses(self):
        self.assertEqual(inputs(expenses_include_emi=False).core_expenses, 2e5)

    def test_crossover_with_contributions_only(self):
        # Need 1000*12/0.04 = 300,000; 10,000/month with no growth or inflation -> 30 months
        i = inputs(investments={}, bank_cash=0, monthly_expenses=1000, expenses_include_emi=False,
                   loan_outstanding=0, emi=0, monthly_investment=10000, swr=0.04, inflation=0, emergency_months=0)
        p = engine.project(i, annual_return=0)
        self.assertEqual(p["crossover_months"], 30)
        self.assertEqual(p["crossover_date"], dt.date(2029, 5, 1))

    def test_already_fi(self):
        self.assertEqual(engine.project(inputs(investments={"equity": 2e8}))["crossover_months"], 0)

    def test_loan_amortises_and_emi_is_freed(self):
        i = inputs(investments={}, bank_cash=0, monthly_investment=0, loan_outstanding=1e5, emi=1e4,
                   loan_rate=0.0, inflation=0, emergency_months=0)
        series = engine.project(i, annual_return=0, years=2)["series"]
        self.assertEqual(series[1]["loan"], 0)
        self.assertEqual(series[1]["corpus"], 2 * 1e4)  # months 11 and 12 after the 10-month payoff
        self.assertEqual(series[2]["corpus"], 14 * 1e4)

    def test_monte_carlo_is_reproducible_and_ordered(self):
        a, b = engine.monte_carlo(inputs(), runs=300), engine.monte_carlo(inputs(), runs=300)
        self.assertEqual(a["bands"], b["bands"])
        for band in a["bands"]:
            self.assertLessEqual(band["p10"], band["p50"])
            self.assertLessEqual(band["p50"], band["p90"])
        probs = [p["prob"] for p in a["prob_by_year"]]
        self.assertEqual(probs, sorted(probs))

    def test_blended_return_and_vol(self):
        i = inputs(investments={"equity": 50, "debt": 50}, bank_cash=0, emergency_months=0)
        self.assertAlmostEqual(i.blended_return, 0.09)
        self.assertTrue(0.03 < i.blended_volatility < 0.18)
        self.assertAlmostEqual(inputs(returns={"equity": 0.2}, bank_cash=0, emergency_months=0).blended_return, 0.2)

    def test_real_hourly_wage(self):
        self.assertEqual(engine.real_hourly_wage(500000, 50000, 225), 2000)
        self.assertEqual(engine.life_energy_hours(150000, 2000), 75)


class LoanBackfillTests(TestCase):
    def test_backward_amortisation_round_trips(self):
        past = services.estimate_past_outstanding(D("5000000"), D("60000"), D("0.085"),
                                                  dt.date(2026, 10, 1), dt.date(2026, 11, 4))
        forward = past * (1 + D("0.085") / 12) - D("60000")
        self.assertAlmostEqual(float(forward), 5000000, delta=1)


class PlanPageTests(TestCase):
    def setUp(self):
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")
        self.p = Person.objects.create(name="Saurabh")
        self.zer = Account.objects.create(owner=self.p, name="Zerodha", kind="broker")
        self.bank = Account.objects.create(owner=self.p, name="Bank", kind="bank", asset_class="cash")
        for date, z in [(dt.date(2026, 9, 1), 5e7), (dt.date(2026, 10, 1), 5.2e7), (dt.date(2026, 11, 4), 5.4e7)]:
            s = Snapshot.objects.create(date=date, flows_tracked=False)
            Balance.objects.create(snapshot=s, account=self.zer, value=z)
            Balance.objects.create(snapshot=s, account=self.bank, value=4e6)

    def form(self, **kw):
        d = {"monthly_expenses": "200000", "includes_emi": "on", "expenses_from": "2026-11-01",
             "monthly_net_income": "600000", "hours_worked_per_month": "220", "work_costs_monthly": "40000",
             "planned_monthly_investment": "150000", "safe_withdrawal_rate": "3.33", "inflation": "6",
             "emergency_fund_months": "12", "target_fi_date": "2030-12-31",
             "loan_owner": self.p.id, "loan_outstanding": "5000000", "loan_emi": "60000", "loan_rate": "8.5",
             "loan_end_date": "2035-01-01", "loan_backfill": "on",
             "target_equity": "70", "target_gold": "10", "target_debt": "15", "target_cash": "5", "target_hybrid": "",
             "band_equity": "5", "return_equity": "11"}
        d.update(kw)
        return d

    def test_redirects_to_plan_without_expenses(self):
        self.assertRedirects(self.client.get("/fi/"), "/plan/")

    def test_save_plan_and_view_fi(self):
        r = self.client.post("/plan/", self.form())
        self.assertRedirects(r, "/fi/")
        hh = Household.get()
        self.assertEqual(hh.safe_withdrawal_rate, D("0.0333"))
        self.assertEqual(MonthlyExpense.objects.get().amount, D("200000"))
        self.assertEqual(AllocationTarget.objects.count(), 4)
        loan = Account.objects.get(kind="loan")
        # outstanding on latest snapshot, estimated for both earlier ones
        self.assertEqual(Balance.objects.filter(account=loan).count(), 3)
        older = Balance.objects.get(account=loan, snapshot__date=dt.date(2026, 9, 1)).value
        self.assertGreater(older, D("5000000"))

        r = self.client.get("/fi/")
        self.assertEqual(r.status_code, 200)
        inp = r.context["inp"]
        self.assertEqual(inp.loan_outstanding, 5e6)
        self.assertEqual(inp.core_expenses, 140000)
        self.assertContains(r, "wall chart")
        self.assertAlmostEqual(r.context["wage"], (600000 - 40000) / 220)

    def test_targets_must_sum_to_100(self):
        r = self.client.post("/plan/", self.form(target_cash="20"))
        self.assertContains(r, "must add up to 100")

    def test_rerunning_backfill_does_not_overwrite(self):
        self.client.post("/plan/", self.form())
        self.client.post("/plan/", self.form(loan_outstanding="4900000"))
        loan = Account.objects.get(kind="loan")
        self.assertEqual(Balance.objects.filter(account=loan).count(), 3)
        self.assertEqual(Balance.objects.get(account=loan, snapshot__date=dt.date(2026, 11, 4)).value, D("4900000"))

    def test_drift(self):
        self.client.post("/plan/", self.form())
        inp, _ = data.build_inputs()
        rows = {d["asset_class"]: d for d in data.drift(inp)}
        self.assertAlmostEqual(rows["equity"]["actual"], 5.4e7 / 5.8e7)
        self.assertTrue(rows["equity"]["out_of_band"])
        self.assertAlmostEqual(sum(d["rebalance_amount"] for d in rows.values()), 0, delta=1)
