import datetime as dt
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Balance, Loan, LoanCheckpoint, LoanPrepayment, LoanRate, Person, Snapshot

from . import schedule, services

START = dt.date(2020, 1, 1)
TODAY = dt.date(2026, 11, 4)


def sch(**kw):
    args = dict(principal=5000000, start_date=START, tenure_months=240,
                rates=[(START, D("0.085"), None)], today=TODAY)
    args.update(kw)
    return schedule.build(**args)


class ScheduleTests(TestCase):
    def test_standard_emi_and_full_term(self):
        self.assertEqual(schedule.annuity(5000000, D("0.085"), 240), D("43391.16"))
        s = sch()
        self.assertEqual(len(s.rows), 240)
        self.assertEqual(s.rows[-1].closing, 0)
        self.assertEqual(s.payoff_date, dt.date(2040, 1, 1))
        self.assertEqual(s.rows[0].interest, D("35416.67"))  # 50L × 8.5% / 12
        self.assertEqual(s.principal_paid + s.outstanding, D("5000000"))

    def test_rate_cut_keeps_emi_and_shortens_tenure(self):
        cut = sch(rates=[(START, D("0.085"), None), (dt.date(2024, 1, 1), D("0.0735"), None)])
        self.assertTrue(all(r.emi == D("43391.16") for r in cut.rows[:-1]))
        self.assertLess(len(cut.rows), 240)
        self.assertIn("rate 7.35%", next(r.note for r in cut.rows if r.date == dt.date(2024, 1, 1)))

    def test_rate_change_with_new_emi(self):
        s = sch(rates=[(START, D("0.085"), None), (dt.date(2024, 1, 1), D("0.076"), D("40000"))])
        self.assertEqual(next(r.emi for r in s.rows if r.date == dt.date(2024, 1, 1)), D("40000"))

    def test_up_down_up_rates(self):
        s = sch(rates=[(START, D("0.076"), None), (dt.date(2025, 1, 1), D("0.0735"), None),
                       (dt.date(2026, 4, 1), D("0.076"), None)])
        rates = {r.date: r.rate for r in s.rows}
        self.assertEqual((rates[dt.date(2024, 12, 1)], rates[dt.date(2025, 6, 1)], rates[dt.date(2026, 6, 1)]),
                         (D("0.076"), D("0.0735"), D("0.076")))

    def test_prepayment_reduce_tenure(self):
        base, pre = sch(), sch(prepayments=[(dt.date(2023, 6, 15), 500000, "tenure")])
        self.assertLess(len(pre.rows), len(base.rows))
        self.assertEqual(pre.prepaid, 500000)
        self.assertLess(sum(r.interest for r in pre.rows), sum(r.interest for r in base.rows))
        self.assertEqual(pre.rows[-1].closing, 0)

    def test_prepayment_reduce_emi_keeps_end_date(self):
        pre = sch(prepayments=[(dt.date(2023, 6, 15), 500000, "emi")])
        self.assertLess(pre.rows[50].emi, D("43391.16"))
        self.assertEqual(len(pre.rows), 240)

    def test_checkpoint_reanchors(self):
        s = sch(checkpoints=[(dt.date(2025, 1, 10), 4000000)])
        row = next(r for r in s.rows if r.date == dt.date(2025, 2, 1))
        self.assertEqual(row.opening, D("4000000"))
        self.assertIn("bank balance", row.note)

    def test_fy_totals_and_outstanding_on(self):
        s = sch()
        fy = s.fy_totals(TODAY)
        self.assertEqual(fy["fy"], "FY2026-27")
        self.assertEqual(fy["emi"], D("43391.16") * 12)
        self.assertEqual(s.outstanding_on(dt.date(2019, 12, 1)), D("5000000"))

    def test_mid_way_start(self):
        """Tracking from today's outstanding and remaining months works the same way."""
        s = sch(principal=3971308, start_date=dt.date(2026, 11, 1), tenure_months=140,
                rates=[(dt.date(2026, 11, 1), D("0.076"), None)], emi=D("43391"))
        self.assertTrue(130 <= len(s.rows) <= 141)


class LoanFlowTests(TestCase):
    def setUp(self):
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")
        self.joint = Person.objects.get(is_joint=True)
        Person.objects.create(name="Saurabh")
        for d in (dt.date(2026, 9, 1), dt.date(2026, 10, 1), dt.date(2026, 11, 4)):
            Snapshot.objects.create(date=d, flows_tracked=False)

    def create(self):
        r = self.client.post("/loans/new/", {
            "name": "Home loan", "owner": self.joint.id, "lender": "HDFC", "loan_type": "home",
            "rate_type": "floating", "initial_rate": "7.6", "principal": "6000000", "start_date": "2022-04-10",
            "tenure_months": "240", "statement_keyword": "HomeLoan"})
        loan = Loan.objects.get()
        self.assertRedirects(r, f"/loans/{loan.pk}/")
        return loan

    def test_create_joint_loan_fills_snapshots_and_syncs(self):
        loan = self.create()
        self.assertEqual(loan.account.owner, self.joint)
        self.assertEqual(loan.rates.get().rate, D("0.076"))
        self.assertEqual(Balance.objects.filter(account=loan.account).count(), 3)
        self.assertEqual(loan.account.interest_rate, D("0.076"))
        self.assertIsNotNone(loan.account.emi)
        page = self.client.get(f"/loans/{loan.pk}/")
        self.assertContains(page, "Amortisation schedule")
        self.assertContains(self.client.get("/loans/"), "Home loan")

    def test_rate_changes_prepayments_and_savings(self):
        loan = self.create()
        self.client.post(f"/loans/{loan.pk}/rates/", {"effective_date": "2024-05-01", "rate": "7.35"})
        self.client.post(f"/loans/{loan.pk}/rates/", {"effective_date": "2025-09-01", "rate": "7.6"})
        self.client.post(f"/loans/{loan.pk}/prepayments/", {"date": "2025-01-20", "amount": "300000", "effect": "tenure"})
        self.assertEqual(loan.rates.count(), 3)
        self.assertEqual(LoanPrepayment.objects.count(), 1)
        sav = schedule.savings_from_prepayments(loan, TODAY)
        self.assertGreater(sav["interest_saved"], 0)
        self.assertGreater(sav["months_saved"], 0)
        self.assertContains(self.client.get(f"/loans/{loan.pk}/"), "save about")

    def test_checkpoint_and_refresh_snapshots(self):
        loan = self.create()
        self.client.post(f"/loans/{loan.pk}/checkpoints/", {"date": "2026-10-05", "outstanding": "5000000"})
        self.assertEqual(LoanCheckpoint.objects.count(), 1)
        self.client.post(f"/loans/{loan.pk}/refresh-snapshots/")
        nov = Balance.objects.get(account=loan.account, snapshot__date=dt.date(2026, 11, 4)).value
        self.assertLess(nov, D("5000000"))  # one more EMI after the bank's figure

    def test_existing_account_gets_details(self):
        acc = services.create_account("Car loan", Person.objects.get(name="Saurabh"))
        r = self.client.post(f"/loans/new/?account={acc.id}", {
            "name": "Car loan", "owner": acc.owner_id, "loan_type": "car", "rate_type": "fixed",
            "initial_rate": "9", "principal": "800000", "start_date": "2024-03-10", "tenure_months": "60",
            "emi": "20606"})
        loan = Loan.objects.get()
        self.assertEqual(loan.account, acc)
        self.assertRedirects(r, f"/loans/{loan.pk}/")

    def test_month_end_form_prefills_loan(self):
        loan = self.create()
        from core.views import _values_from_pulls
        v = _values_from_pulls([loan.account], dt.date(2026, 11, 30))[loan.account.id]
        self.assertEqual(v, schedule.for_loan(loan).outstanding_on(dt.date(2026, 11, 30)).quantize(D("0.01")))

    def test_joint_is_not_a_person_for_transfers_or_zerodha(self):
        from statements.rules import family_names
        self.assertNotIn("JOINT", family_names())
