import datetime as dt
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core import analytics
from core.models import (Account, Balance, CashFlow, HoldingsPull, Instrument, KiteSession, NavPrice,
                         Person, Snapshot)
from core.xirr import xirr

from . import amfi, cas, classify, kite, zerodha_ledger


class FakeKite:
    def holdings(self):
        return [
            {"tradingsymbol": "RELIANCE", "isin": "INE002A01018", "quantity": 10, "t1_quantity": 2,
             "average_price": 2400, "last_price": 2500, "close_price": 2490},
            {"tradingsymbol": "GOLDBEES", "isin": "INF204KB17I5", "quantity": 1000, "t1_quantity": 0,
             "average_price": 50, "last_price": 60},
            {"tradingsymbol": "SGBAUG28V-GB", "isin": "IN0020200153", "quantity": 5, "t1_quantity": 0,
             "average_price": 5000, "last_price": 7000},
            {"tradingsymbol": "SOLD", "isin": "X", "quantity": 0, "t1_quantity": 0, "last_price": 1},
        ]

    def mf_holdings(self):
        return [{"tradingsymbol": "INF879O01027", "fund": "PARAG PARIKH FLEXI CAP FUND - DIRECT PLAN",
                 "quantity": 100, "average_price": 50, "last_price": 80}]

    def margins(self):
        return {"equity": {"net": 12345.5}}


class ClassifyTests(TestCase):
    def test_rules(self):
        self.assertEqual(classify.asset_class_for("GOLDBEES", "GOLDBEES"), "gold")
        self.assertEqual(classify.kind_for_symbol("SGBAUG28V-GB"), Instrument.Kind.SGB)
        self.assertEqual(classify.asset_class_for("LIQUIDBEES", "LIQUIDBEES"), "debt")
        self.assertEqual(classify.asset_class_for("HDFC Balanced Advantage Fund"), "hybrid")
        self.assertEqual(classify.asset_class_for("Parag Parikh Flexi Cap Fund"), "equity")
        self.assertTrue(classify.is_passive("UTI Nifty 50 Index Fund - Direct"))
        self.assertFalse(classify.is_passive("Mirae Asset Large Cap Fund"))

    def test_default_management(self):
        stock = classify.get_or_create_instrument("RELIANCE", "RELIANCE", Instrument.Kind.STOCK)
        index = classify.get_or_create_instrument("INF1", "UTI Nifty 50 Index Fund", Instrument.Kind.MF,
                                                  isin="INF1", via_et_money=True)
        active = classify.get_or_create_instrument("INF2", "Mirae Asset Large Cap Fund", Instrument.Kind.MF,
                                                   isin="INF2", via_et_money=True)
        self.assertEqual((stock.management, index.management, active.management), ("advisor", "advisor", "self"))
        self.assertEqual(index.advisor, "ET Money")
        self.assertTrue(all(i.needs_review for i in (stock, index, active)))

    def test_existing_tags_are_not_overwritten(self):
        inst = classify.get_or_create_instrument("TCS", "TCS", Instrument.Kind.STOCK)
        inst.management, inst.needs_review = "self", False
        inst.save()
        again = classify.get_or_create_instrument("TCS", "TCS", Instrument.Kind.STOCK, isin="INE467B01029")
        self.assertEqual((again.management, again.needs_review, again.isin), ("self", False, "INE467B01029"))


class KiteTests(TestCase):
    def setUp(self):
        self.person = Person.objects.create(name="Saurabh")

    def test_pull_holdings(self):
        pull = kite.pull_holdings(self.person, kite=FakeKite())
        values = {p.instrument.identifier: p.value for p in pull.positions.select_related("instrument")}
        self.assertEqual(values["RELIANCE"], D("30000.00"))  # 12 incl. T1 × 2500
        self.assertEqual(values["GOLDBEES"], D("60000.00"))
        self.assertEqual(values["INF879O01027"], D("8000.00"))
        self.assertEqual(values[kite.CASH_ID], D("12345.5"))
        self.assertNotIn("SOLD", values)
        self.assertEqual(pull.total_value, D("30000") + D("60000") + D("35000") + D("8000") + D("12345.5"))
        self.assertEqual(pull.account.kind, "broker")
        gold = Instrument.objects.get(identifier="SGBAUG28V-GB")
        self.assertEqual((gold.kind, gold.asset_class, gold.management), ("sgb", "gold", "self"))

    def test_token_expires_at_six_am(self):
        tz = timezone.get_current_timezone()
        s = KiteSession.objects.create(person=self.person, access_token="x",
                                       created_at=dt.datetime(2026, 11, 3, 22, 0, tzinfo=tz))
        with mock.patch("django.utils.timezone.now", return_value=dt.datetime(2026, 11, 4, 5, 59, tzinfo=tz)):
            self.assertTrue(kite._token_valid(s))
        with mock.patch("django.utils.timezone.now", return_value=dt.datetime(2026, 11, 4, 6, 1, tzinfo=tz)):
            self.assertFalse(kite._token_valid(s))

    def test_not_configured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(kite.is_configured(self.person))


LEDGER = """particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance
Opening Balance,,,,,,0
Funds added using UPI with reference number 123,2026-11-02,NSE-EQ - Z,Bank Receipts,0,"50,000.00",50000
Net settlement for Equity with settlement number: 2026210,2026-11-03,NSE-EQ - Z,Book Voucher,49000,0,1000
Payout of 1000 to bank,2026-11-05,NSE-EQ - Z,Bank Payments,1000,0,0
DP Charges,2026-11-06,NSE-EQ - Z,Journal Entry,15.93,0,-15.93
"""


class LedgerTests(TestCase):
    def test_import_payins_and_payouts_only(self):
        acc = Account.objects.create(owner=Person.objects.create(name="A"), name="Zerodha", kind="broker")
        self.assertEqual(zerodha_ledger.import_ledger(acc, LEDGER), (2, 0))
        self.assertEqual(sorted(CashFlow.objects.values_list("amount", flat=True)), [D("-1000"), D("50000")])
        self.assertEqual(zerodha_ledger.import_ledger(acc, LEDGER), (0, 2))  # re-upload is safe


def cas_data(units="100", value="5000"):
    return {
        "statement_period": {"from": "01-Apr-2026", "to": "31-Oct-2026"},
        "folios": [{"folio": "123/45", "amc": "UTI", "schemes": [
            {"scheme": "UTI Nifty 50 Index Fund - Direct Growth", "isin": "INF789F1AUV0", "amfi": "120716",
             "rta_code": "X", "rta": "KFINTECH", "open": "0", "close": units, "close_calculated": units,
             "valuation": {"date": "2026-10-31", "nav": "50", "value": value, "cost": "4000"},
             "transactions": [
                 {"date": "2026-10-05", "description": "SIP", "amount": "3000", "units": "60", "type": "PURCHASE_SIP"},
                 {"date": "2026-10-06", "description": "Stamp", "amount": "0.15", "type": "STAMP_DUTY_TAX"},
                 {"date": "2026-10-20", "description": "Redeem", "amount": "-1000", "units": "-20", "type": "REDEMPTION"},
                 {"date": "2026-10-21", "description": "Switch in", "amount": "2000", "units": "40", "type": "SWITCH_IN"},
             ]},
            {"scheme": "Old Fund - Regular", "isin": "INF000000001", "amfi": "100001", "rta_code": "Y",
             "rta": "CAMS", "open": "10", "close": "0", "close_calculated": "0",
             "valuation": {"date": "2026-10-31", "nav": "10", "value": "0"}, "transactions": []},
        ]}],
    }


class CasTests(TestCase):
    def setUp(self):
        self.acc = Account.objects.create(owner=Person.objects.create(name="A"), name="ET Money", kind="mf")

    def test_import_positions_and_flows(self):
        pull, created, skipped = cas.import_cas(cas_data(), self.acc)
        self.assertEqual((pull.total_value, pull.positions.count(), pull.as_of), (D("5000"), 1, dt.date(2026, 10, 31)))
        flows = dict(CashFlow.objects.values_list("note", "amount"))
        self.assertEqual(sorted(flows.values()), [D("-1000"), D("2000"), D("3000")])
        self.assertEqual(CashFlow.objects.get(amount=2000).kind, "switch")
        inst = Instrument.objects.get(isin="INF789F1AUV0")
        self.assertEqual((inst.management, inst.amfi_code, inst.last_price), ("advisor", "120716", D("50")))
        # Re-import: no duplicate flows
        _, created2, skipped2 = cas.import_cas(cas_data(), self.acc)
        self.assertEqual((created, created2, skipped2), (3, 0, 3))

    def test_reads_casparser_typed_result(self):
        parsed = SimpleNamespace(model_dump=cas_data)
        with mock.patch("casparser.read_cas_pdf", return_value=parsed):
            pull, _, _ = cas.import_cas(cas.read_pdf("x.pdf", "pw"), self.acc)
        self.assertEqual(pull.total_value, D("5000"))

    def mixed_statement(self):
        data = cas_data()
        own = data["folios"][0]
        own["name"] = "A B Sharma"
        data["investor_info"] = {"name": "A BHARAT SHARMA"}
        zerodha = {"scheme": "Coin Fund - Direct", "isin": "INF000000002", "amfi": "", "advisor": cas.ZERODHA_CODE,
                   "close": "100", "valuation": {"date": "2026-10-31", "nav": "10", "value": "1000", "cost": "800"},
                   "transactions": [{"date": "2026-10-02", "type": "PURCHASE", "amount": "800", "units": "80"}]}
        child = {"scheme": "Child Fund - Direct", "isin": "INF000000003", "amfi": "", "advisor": cas.ZERODHA_CODE,
                 "close": "50", "valuation": {"date": "2026-10-31", "nav": "10", "value": "500", "cost": "400"},
                 "transactions": [{"date": "2026-10-02", "type": "PURCHASE", "amount": "400", "units": "40"}]}
        data["folios"] += [{"folio": "22", "name": "A Sharma", "schemes": [zerodha]},
                           {"folio": "33", "name": "Kid Sharma", "schemes": [child]}]
        return data

    def test_zerodha_funds_keep_flows_but_not_holdings(self):
        broker = Account.objects.create(owner=self.acc.owner, name="Zerodha", kind="broker")
        pull, _, _ = cas.import_cas(self.mixed_statement(), self.acc)
        self.assertEqual((pull.total_value, pull.positions.count()), (D("5000"), 1))
        self.assertEqual(CashFlow.objects.get(account=broker).amount, D("800"))

    def test_folios_in_another_name_are_left_out_and_reported(self):
        data = self.mixed_statement()
        cas.import_cas(data, self.acc)
        self.assertFalse(Instrument.objects.filter(isin="INF000000003").exists())
        self.assertEqual(cas.left_out(data), ({"Kid Sharma": D("500")}, D("1000")))

    def test_amfi_nav_revalues_pull(self):
        pull, _, _ = cas.import_cas(cas_data(), self.acc)
        text = ("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date\n"
                "Open Ended Schemes(Index Funds)\n"
                "120716;INF789F1AUV0;-;UTI Nifty 50 Index Fund - Direct;55.0000;04-Nov-2026\n"
                "999999;INF999;-;Unrelated;10;04-Nov-2026\n")
        # current 8-column layout (Plan, Option) must parse too
        rows = list(amfi.parse("135762;INF846K01WO1;-;Axis Children's Fund;Direct Plan;Growth Option;29.8188;03-Nov-2026"))
        self.assertEqual(rows[0][0], "135762")
        self.assertEqual(rows[0][3], D("29.8188"))
        self.assertIn("Direct Plan", rows[0][2])
        self.assertEqual(amfi.update_navs(text), 1)
        self.assertEqual(NavPrice.objects.count(), 1)
        self.assertEqual(pull.current_value(), D("5500.00"))


class XirrTests(TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(float(xirr([(dt.date(2025, 1, 1), -1000), (dt.date(2026, 1, 1), 1100)])), 0.10, places=4)
        self.assertIsNone(xirr([(dt.date(2025, 1, 1), -1000)]))

    def test_account_xirr_uses_tracked_months_only(self):
        p = Person.objects.create(name="A")
        acc = Account.objects.create(owner=p, name="Z", kind="broker")
        for date, val, tracked in [(dt.date(2025, 1, 1), 500, False), (dt.date(2026, 1, 1), 1000, False),
                                   (dt.date(2027, 1, 1), 1200, True)]:
            s = Snapshot.objects.create(date=date, flows_tracked=tracked)
            Balance.objects.create(snapshot=s, account=acc, value=val)
        CashFlow.objects.create(date=dt.date(2026, 7, 2), account=acc, amount=100)
        r = analytics.xirr_for([acc])
        self.assertTrue(0.08 < float(r) < 0.11, r)  # 1000 + 100 mid-year -> 1200


class HoldingsFlowTests(TestCase):
    """End to end: Kite pull + ledger feed the month-end form and the allocation."""

    def setUp(self):
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")
        self.person = Person.objects.create(name="Saurabh")
        self.zer = kite.zerodha_account(self.person)
        self.bank = Account.objects.create(owner=self.person, name="Bank", kind="bank", asset_class="cash")
        s = Snapshot.objects.create(date=dt.date.today() - dt.timedelta(days=30), flows_tracked=False)
        Balance.objects.create(snapshot=s, account=self.zer, value=100000)
        Balance.objects.create(snapshot=s, account=self.bank, value=50000)

    def test_form_prefills_from_pull_and_ledger_and_links_pull(self):
        pull = kite.pull_holdings(self.person, kite=FakeKite())
        CashFlow.objects.create(date=dt.date.today() - dt.timedelta(days=3), account=self.zer,
                                amount=20000, source="kite", external_id="kite:t1")
        r = self.client.get("/entry/")
        self.assertEqual(r.context["form"][f"value_{self.zer.id}"].value(), pull.total_value)
        self.assertEqual(r.context["form"][f"flow_{self.zer.id}"].value(), D("20000"))
        r = self.client.post("/entry/", {
            "date": dt.date.today().isoformat(), "note": "",
            f"value_{self.zer.id}": str(pull.total_value), f"value_{self.bank.id}": "30000",
            f"flow_{self.zer.id}": "20000",
        })
        self.assertRedirects(r, "/")
        self.assertEqual(CashFlow.objects.count(), 1)  # ledger flow not duplicated
        snap = Snapshot.objects.latest("date")
        self.assertEqual(snap.balances.get(account=self.zer).pull, pull)
        alloc = {a["asset_class"]: a["value"] for a in analytics.allocation(snap)}
        self.assertAlmostEqual(float(alloc["gold"]), 95000, delta=1)  # GOLDBEES + SGB
        self.assertAlmostEqual(float(sum(alloc.values())), float(pull.total_value) + 30000, delta=1)
        p = analytics.periods()[-1]
        self.assertEqual(p.investments.invested, D("20000"))

    def test_holdings_page_and_tag_save(self):
        kite.pull_holdings(self.person, kite=FakeKite())
        r = self.client.get("/holdings/")
        self.assertContains(r, "RELIANCE")
        inst = Instrument.objects.get(identifier="RELIANCE")
        self.client.post("/holdings/tags/", {"instrument": [inst.id], f"mgmt_{inst.id}": "self",
                                             f"advisor_{inst.id}": ""})
        inst.refresh_from_db()
        self.assertEqual((inst.management, inst.needs_review), ("self", False))
