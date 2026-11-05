import datetime as dt

from django.contrib.auth.models import User
from django.test import TestCase

from core import checklist_views as cl
from core.models import Account, CashFlow, Person, Snapshot, StatementUpload

TODAY = dt.date(2026, 11, 5)
D = dt.date


class ChecklistTests(TestCase):
    def setUp(self):
        self.s = Person.objects.create(name="Saurabh")
        self.bank = Account.objects.create(owner=self.s, name="Bank X", kind="bank", asset_class="cash")
        self.card = Account.objects.create(owner=self.s, name="Card Y", kind="card", asset_class="liability")
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")

    def upload(self, acc, start, end):
        StatementUpload.objects.create(account=acc, filename="s.csv", sha256=f"{acc.id}{start}", parser="table",
                                       period_start=start, period_end=end)

    def row(self, acc):
        return next(r for r in cl.statement_grid(TODAY)["rows"] if r["account"] == acc)

    def test_months_run_from_start_to_last_complete_month(self):
        months = cl.statement_grid(TODAY)["months"]
        self.assertEqual((months[0], months[-1], len(months)), (D(2025, 6, 1), D(2026, 10, 1), 17))

    def test_gaps_are_grouped_into_ranges(self):
        self.upload(self.bank, D(2025, 6, 1), D(2026, 3, 31))
        self.upload(self.bank, D(2026, 6, 1), D(2026, 9, 30))
        self.assertEqual(self.row(self.bank)["ranges"], ["Apr 2026 – May 2026", "Oct 2026"])

    def test_card_cycles_within_grace_count_as_covered(self):
        self.upload(self.card, D(2026, 9, 2), D(2026, 10, 1))
        cells = dict(zip(cl.statement_grid(TODAY)["months"], self.row(self.card)["cells"]))
        self.assertEqual(cells[D(2026, 9, 1)]["state"], cl.OK)        # 29 of 30 days
        self.assertEqual(cells[D(2026, 10, 1)]["state"], cl.PARTIAL)  # 1 of 31 days
        self.assertEqual(cells[D(2026, 8, 1)]["state"], cl.MISSING)

    def test_needed_from_and_archive_stop_the_nagging(self):
        self.client.post(f"/checklist/from/{self.card.id}/", {"statements_from": "2026-10-01"})
        self.card.refresh_from_db()
        row = self.row(self.card)
        self.assertEqual(row["ranges"], ["Oct 2026"])
        self.assertEqual(row["cells"][0]["state"], "na")
        self.client.post(f"/checklist/from/{self.card.id}/", {"statements_from": ""})
        self.card.refresh_from_db()
        self.assertIsNone(self.card.statements_from)
        self.bank.is_active = False
        self.bank.save()
        self.assertEqual([r["account"] for r in cl.statement_grid(TODAY)["rows"]], [self.card])

    def test_results_and_todos(self):
        zer = Account.objects.create(owner=self.s, name="Zerodha", institution="Zerodha", kind="broker")
        self.upload(self.bank, D(2025, 6, 1), D(2026, 10, 31))
        _, r = cl.run_check(TODAY)
        self.assertEqual(r["bank"].status, cl.OK)
        self.assertEqual(r["card"].status, cl.MISSING)
        self.assertIn("Card Y: Jun 2025 – Oct 2026", r["card"].todos[0][0])
        self.assertEqual(r["ledger"].status, cl.MISSING)
        CashFlow.objects.create(account=zer, date=D(2026, 10, 20), amount=1000, source="kite", kind="invest")
        self.assertEqual(cl.run_check(TODAY)[1]["ledger"].status, cl.OK)
        self.assertEqual(r["monthend"].status, cl.MISSING)
        Snapshot.objects.create(date=D(2026, 11, 1))
        self.assertEqual(cl.run_check(TODAY)[1]["monthend"].status, cl.OK)

    def test_page_runs_the_check_only_on_request(self):
        page = self.client.get("/checklist/")
        self.assertContains(page, "Credit card statements")
        self.assertContains(page, "Good to have")
        self.assertNotContains(page, "To upload")
        page = self.client.get("/checklist/?check=1")
        self.assertContains(page, "To upload")
        self.assertContains(page, "Card Y")
