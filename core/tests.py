import datetime as dt
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase

from . import analytics
from .importer import import_workbook
from .models import Account, Balance, CashFlow, Person, Snapshot
from .templatetags.money import inr

SHEET = Path(settings.BASE_DIR) / "docs" / "net_worth.xlsx"
D = Decimal


import unittest


@unittest.skipUnless(SHEET.exists(), "docs/net_worth.xlsx is private and not in git")
class ImporterTests(TestCase):
    def test_every_imported_month_matches_sheet_total(self):
        report = import_workbook(SHEET)
        self.assertGreaterEqual(len(report), 19)
        for date, imported, sheet in report:
            self.assertAlmostEqual(float(imported), float(sheet), delta=1, msg=str(date))

    def test_reimport_is_idempotent(self):
        import_workbook(SHEET)
        n = Balance.objects.count()
        import_workbook(SHEET)
        self.assertEqual(Balance.objects.count(), n)

    def test_imported_months_are_marked_untracked(self):
        import_workbook(SHEET)
        self.assertFalse(Snapshot.objects.filter(flows_tracked=True).exists())
        latest = analytics.periods()[-1]
        self.assertIsNone(latest.market_gain)
        self.assertAlmostEqual(float(latest.net_worth), 67124730.4, delta=1)


class AttributionTests(TestCase):
    def setUp(self):
        self.p = Person.objects.create(name="A")
        self.bank = Account.objects.create(owner=self.p, name="Bank", kind="bank", asset_class="cash")
        self.zer = Account.objects.create(owner=self.p, name="Zerodha", kind="broker")
        self.fd = Account.objects.create(owner=self.p, name="FD", kind="fd", asset_class="debt")
        self.loan = Account.objects.create(owner=self.p, name="HL", kind="loan", asset_class="liability")

    def snap(self, date, **vals):
        s = Snapshot.objects.create(date=date)
        for name, v in vals.items():
            Balance.objects.create(snapshot=s, account=getattr(self, name), value=D(v))
        return s

    def test_invested_and_gain_split(self):
        self.snap(dt.date(2026, 1, 31), bank=500, zer=1000, fd=300, loan=200)
        # Moved 100 from bank into Zerodha; Zerodha ended at 1150 -> market gain 50.
        CashFlow.objects.create(date=dt.date(2026, 2, 10), account=self.zer, amount=D(100), from_account=self.bank)
        self.snap(dt.date(2026, 2, 28), bank=450, zer=1150, fd=300, loan=190)
        p = analytics.periods()[-1]
        self.assertEqual(p.investments.invested, D(100))
        self.assertEqual(p.market_gain, D(50))
        self.assertEqual(p.net_worth, D(450 + 1150 + 300 - 190))
        # opening + invested + gain == closing for every account
        for b in p.by_account.values():
            self.assertEqual(b.opening + b.invested + b.gain, b.closing)
        # NW change = market gain + savings
        self.assertEqual(p.change, p.market_gain + p.savings)

    def test_switch_between_investments_nets_to_zero(self):
        self.snap(dt.date(2026, 1, 31), zer=1000, fd=300)
        CashFlow.objects.create(date=dt.date(2026, 2, 5), account=self.fd, amount=D(-300), kind="switch")
        CashFlow.objects.create(date=dt.date(2026, 2, 5), account=self.zer, amount=D(300), kind="switch")
        self.snap(dt.date(2026, 2, 28), zer=1280, fd=0)
        p = analytics.periods()[-1]
        self.assertEqual(p.investments.invested, D(0))
        self.assertEqual(p.market_gain, D(-20))

    def test_flows_outside_period_ignored(self):
        self.snap(dt.date(2026, 1, 31), zer=1000)
        CashFlow.objects.create(date=dt.date(2026, 1, 31), account=self.zer, amount=D(999))  # belongs to Jan
        self.snap(dt.date(2026, 2, 28), zer=1010)
        self.assertEqual(analytics.periods()[-1].market_gain, D(10))

    def test_allocation_excludes_loans(self):
        s = self.snap(dt.date(2026, 1, 31), bank=100, zer=300, loan=50)
        alloc = {a["asset_class"]: a["value"] for a in analytics.allocation(s)}
        self.assertEqual(alloc, {"equity": D(300), "cash": D(100)})


class EntryFormTests(TestCase):
    def setUp(self):
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")
        p = Person.objects.create(name="A")
        self.bank = Account.objects.create(owner=p, name="Bank", kind="bank", asset_class="cash")
        self.zer = Account.objects.create(owner=p, name="Zerodha", kind="broker")
        s = Snapshot.objects.create(date=dt.date(2026, 1, 31), flows_tracked=False)
        Balance.objects.create(snapshot=s, account=self.bank, value=500)
        Balance.objects.create(snapshot=s, account=self.zer, value=1000)

    def post(self, url, **extra):
        data = {"date": "2026-02-28", "note": "",
                f"value_{self.bank.id}": "400", f"value_{self.zer.id}": "1130",
                f"flow_{self.zer.id}": "100"}
        data.update(extra)
        return self.client.post(url, data)

    def test_save_records_values_and_money_invested(self):
        r = self.post("/entry/")
        self.assertRedirects(r, "/")
        p = analytics.periods()[-1]
        self.assertEqual(p.investments.invested, D(100))
        self.assertEqual(p.market_gain, D(30))

    def test_editing_does_not_double_count_flows(self):
        self.post("/entry/")
        self.post("/entry/2026-02-28/", **{f"flow_{self.zer.id}": "120"})
        self.assertEqual(CashFlow.objects.count(), 1)
        self.assertEqual(analytics.periods()[-1].investments.invested, D(120))

    def test_detailed_flows_are_not_duplicated_by_form(self):
        CashFlow.objects.create(date=dt.date(2026, 2, 10), account=self.zer, amount=D(60))
        self.post("/entry/")  # form total says 100 -> stores the remaining 40
        self.assertEqual(analytics.periods()[-1].investments.invested, D(100))

    def test_dashboard_renders(self):
        self.post("/entry/")
        r = self.client.get("/")
        self.assertContains(r, "Market gain")


class FormatTests(TestCase):
    def test_inr(self):
        self.assertEqual(inr(D("67124730.4")), "₹6.71 Cr")
        self.assertEqual(inr(D("-250000")), "−₹2.50 L")
        self.assertEqual(inr(D("45000")), "₹45,000")
