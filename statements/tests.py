import datetime as dt
import io
import json
from decimal import Decimal as D
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from core import analytics
from core.models import (Account, Balance, CashFlow, CategoryRule, Instrument, MonthlyExpense, Person, Snapshot,
                         StatementUpload, Transaction)
from integrations import zerodha_ledger

from . import importer, matching, parse_ai, parse_table, reimburse, rules, spending

HDFC_CSV = """HDFC BANK Ltd.
Statement of account
Account No :XXXX1234

Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance
01/10/26,UPI-TO ZERODHA BROKING LTD-ZERODHA@HDFC-123,0000123,01/10/26,"50,000.00",,"4,50,000.00"
02/10/26,UPI-SWIGGY-SWIGGY@ICICI-456,0000456,02/10/26,650.00,,"4,49,350.00"
03/10/26,NEFT CR-ACME CORP SALARY OCT,0000789,03/10/26,,"3,00,000.00","7,49,350.00"
05/10/26,IMPS-TO PRITIKA SAVINGS,0000999,05/10/26,"1,00,000.00",,"6,49,350.00"
06/10/26,ACH D- ETMONEY SIP-ICCL,0001000,06/10/26,"25,000.00",,"6,24,350.00"
07/10/26,LIC PREMIUM PAYMENT,0001001,07/10/26,"12,000.00",,"6,12,350.00"
08/10/26,CRED CLUB CARD BILL,0001002,08/10/26,"40,000.00",,"5,72,350.00"
31/10/26,HOME LOAN EMI OCT,0001003,31/10/26,"70,000.00",,"5,02,350.00"
**** End of statement ****
"""

CARD_CSV = """Transaction Date,Details,Amount (INR)
04/10/2026,ZOMATO ONLINE ORDER,"1,200.00"
09/10/2026,PAYMENT RECEIVED - THANK YOU,"40,000.00 Cr"
12/10/2026,AMAZON PAY INDIA,"3,500.00"
15/10/2026,AMAZON REFUND,"500.00 Cr"
"""


class Setup(TestCase):
    def setUp(self):
        self.s = Person.objects.create(name="Saurabh")
        self.p = Person.objects.create(name="Pritika")
        self.bank = Account.objects.create(owner=self.s, name="Bank HDFC", kind="bank", asset_class="cash")
        self.pbank = Account.objects.create(owner=self.p, name="Bank ICICI", kind="bank", asset_class="cash")
        self.card = Account.objects.create(owner=self.s, name="HDFC Regalia", kind="card", asset_class="liability")
        self.zer = Account.objects.create(owner=self.s, name="Zerodha", institution="Zerodha", kind="broker")
        self.mf = Account.objects.create(owner=self.s, name="ET Money", kind="mf")

    def imp(self, account, name, text):
        return importer.import_statement(account, name, text.encode(), client=NoClaude())


class NoClaude:
    """Fails loudly if anything tries to call Claude."""

    def __getattr__(self, name):
        raise AssertionError("Claude should not be called here")


class TableParserTests(TestCase):
    def test_bank_csv_with_preamble(self):
        rows = parse_table.parse("hdfc.csv", HDFC_CSV.encode(), is_card=False)
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]["date"], dt.date(2026, 10, 1))
        self.assertEqual(rows[0]["amount"], D("-50000.00"))
        self.assertEqual(rows[2]["amount"], D("300000.00"))
        self.assertEqual(rows[0]["balance"], D("450000.00"))

    def test_card_csv_cr_suffix(self):
        rows = parse_table.parse("card.csv", CARD_CSV.encode(), is_card=True)
        self.assertEqual([r["amount"] for r in rows], [D("-1200"), D("40000"), D("-3500"), D("500")])

    def test_xlsx(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Kotak statement"])
        ws.append(["Transaction Date", "Description", "Debit", "Credit", "Balance"])
        ws.append([dt.datetime(2026, 10, 3), "UPI ZEPTO", 800, None, 10000])
        buf = io.BytesIO()
        wb.save(buf)
        rows = parse_table.parse("k.xlsx", buf.getvalue(), is_card=False)
        self.assertEqual((rows[0]["date"], rows[0]["amount"]), (dt.date(2026, 10, 3), D("-800")))

    def test_printed_periods(self):
        rows = [["Statement From  :  01/10/2026  To  :  31/10/2026"], ["Date", "Narration"]]
        self.assertEqual(parse_table.find_period(rows, 2), (dt.date(2026, 10, 1), dt.date(2026, 10, 31)))
        card = [["Statement Date", "01/10/2026"]]
        self.assertEqual(parse_table.find_period(card, 1, is_card=True), (dt.date(2026, 9, 2), dt.date(2026, 10, 1)))

    def test_hdfc_card_tilde_format(self):
        text = ("Statement Date~|~01/10/2026 \n\nTransaction type~|~Primary / Addon Customer Name~|~DATE~|~Description~|~AMT~|~Debit /Credit~|~\n"
                "Domestic~|~S J ~|~06/09/2026 09:09:54~|~WWWBIGBASKETCOM ~|~1,234.56~|~~|~\n"
                "Domestic~|~S J ~|~21/09/2026 09:12:23~|~AUTOPAY THANK YOU~|~10,000.00~|~Cr~|~\n")
        rows, period = parse_table.parse("c.csv", text.encode(), is_card=True, with_period=True)
        self.assertEqual([r["amount"] for r in rows], [D("-1234.56"), D("10000.00")])
        self.assertEqual(period, (dt.date(2026, 9, 2), dt.date(2026, 10, 1)))

    def test_hdfc_billed_statement_layout(self):
        """Debit / Credit marker column (blank = debit) and a statement date written out in words."""
        csv = ('Name,TEST USER\nPayment Due Date,"21 Nov, 2026"\nStatement Date,"01 Nov, 2026"\n\n'
               'Transaction type,Date & Time,Description,AMT,Debit / Credit\n'
               'Domestic,04/10/2026 12:00:00,SHOP ONE,100.00,\n'
               'Domestic,20/10/2026 09:00:00,SHOP ONE REFUND,1.50,Cr\n'
               'Domestic,21/10/2026 10:00:00,AUTOPAY THANK YOU,50.00,Cr\n')
        rows, period = parse_table.parse("billed.csv", csv.encode(), is_card=True, with_period=True)
        self.assertEqual([r["amount"] for r in rows], [D("-100.00"), D("1.50"), D("50.00")])
        self.assertEqual(period, (dt.date(2026, 10, 2), dt.date(2026, 11, 1)))

    def test_ambiguous_single_amount_bank(self):
        with self.assertRaises(parse_table.TableParseError):
            parse_table.parse("x.csv", b"Date,Narration,Amount\n01/10/26,THING,100\n", is_card=False)


class RuleTests(Setup):
    def test_import_categorises_with_rules(self):
        stmt = self.imp(self.bank, "hdfc.csv", HDFC_CSV)
        cats = {t.description[:20]: (t.category, t.spend_category, t.investment_account_id)
                for t in stmt.transactions.all()}
        self.assertEqual(cats["UPI-TO ZERODHA BROKI"], ("investment", "", self.zer.id))
        self.assertEqual(cats["ACH D- ETMONEY SIP-I"], ("investment", "", self.mf.id))
        self.assertEqual(cats["UPI-SWIGGY-SWIGGY@IC"], ("expense", "dining", None))
        self.assertEqual(cats["NEFT CR-ACME CORP SA"][0], "income")
        self.assertEqual(cats["IMPS-TO PRITIKA SAVI"][0], "transfer")      # family name
        self.assertEqual(cats["LIC PREMIUM PAYMENT"][:2], ("expense", "insurance"))  # 'PREMIUM' isn't an EMI
        self.assertEqual(cats["CRED CLUB CARD BILL"][0], "card_payment")
        self.assertEqual(cats["HOME LOAN EMI OCT"][0], "loan_emi")

    def test_subscription_is_not_investment(self):
        rule = rules.match_rule(rules.ordered_rules(), "WEEKEND INVESTING SMALLCASE SUBSCRIPTION", D("-4999"), self.bank)
        self.assertEqual((rule.category, rule.spend_category), ("expense", "subscriptions"))

    def test_keyword_guess(self):
        self.assertEqual(rules.keyword_guess("UPI/DR/412345678/SWIGGY/YESB/swiggy@yes"), "SWIGGY")


class DedupeTests(Setup):
    def test_same_file_rejected_and_overlap_skipped(self):
        self.imp(self.bank, "a.csv", HDFC_CSV)
        with self.assertRaises(importer.ImportError_):
            self.imp(self.bank, "a.csv", HDFC_CSV)
        overlap = HDFC_CSV.replace("**** End", "01/11/26,UPI-ZEPTO,0002000,01/11/26,300.00,,\"5,02,050.00\"\n**** End")
        stmt = self.imp(self.bank, "b.csv", overlap)
        self.assertEqual((stmt.added, stmt.duplicates), (1, 8))


class MatchingTests(Setup):
    def test_card_payment_pairs_with_card_statement(self):
        self.imp(self.bank, "hdfc.csv", HDFC_CSV)
        self.imp(self.card, "card.csv", CARD_CSV)
        bank_side = Transaction.objects.get(description__startswith="CRED CLUB")
        card_side = Transaction.objects.get(description__startswith="PAYMENT RECEIVED")
        self.assertEqual(bank_side.matched_with, card_side)
        self.assertEqual(card_side.category, "card_payment")

    def test_transfer_between_spouses_pairs(self):
        self.imp(self.bank, "hdfc.csv", HDFC_CSV)
        self.imp(self.pbank, "icici.csv", "Date,Description,Debit,Credit\n05/10/2026,IMPS FROM S JOSHI,,100000\n")
        incoming = Transaction.objects.get(account=self.pbank)
        self.assertEqual((incoming.category, incoming.matched_with.account), ("transfer", self.bank))

    def test_bank_investment_then_ledger_counts_once(self):
        snap0 = Snapshot.objects.create(date=dt.date(2026, 9, 30))
        Balance.objects.create(snapshot=snap0, account=self.zer, value=1000000)
        self.imp(self.bank, "hdfc.csv", HDFC_CSV)
        t = Transaction.objects.get(description__contains="ZERODHA")
        matching.confirm(t)
        self.assertEqual(CashFlow.objects.get().source, "bank")
        ledger = ("particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance\n"
                  "Funds added using UPI,2026-10-02,NSE-EQ,Bank Receipts,0,50000,50000\n")
        zerodha_ledger.import_ledger(self.zer, ledger)
        flows = list(CashFlow.objects.filter(account=self.zer))
        self.assertEqual([(f.source, f.amount) for f in flows], [("kite", D("50000"))])
        t.refresh_from_db()
        self.assertEqual(t.cash_flow, flows[0])
        snap1 = Snapshot.objects.create(date=dt.date(2026, 10, 31))
        Balance.objects.create(snapshot=snap1, account=self.zer, value=1060000)
        p = analytics.periods()[-1]
        self.assertEqual((p.investments.invested, p.market_gain), (D("50000"), D("10000")))

    def test_cas_purchases_sum_to_bank_debit(self):
        self.imp(self.bank, "hdfc.csv", HDFC_CSV)
        inst = Instrument.objects.create(identifier="F1", kind="mf")
        CashFlow.objects.create(date=dt.date(2026, 10, 7), account=self.mf, amount=D("14999.25"), source="cas", instrument=inst, external_id="cas:a")
        CashFlow.objects.create(date=dt.date(2026, 10, 7), account=self.mf, amount=D("9999.50"), source="cas", instrument=inst, external_id="cas:b")
        t = Transaction.objects.get(description__contains="ETMONEY")
        matching.confirm(t)  # 25,000 debit vs 24,998.75 of purchases (stamp duty)
        self.assertFalse(CashFlow.objects.filter(source="bank").exists())
        self.assertEqual(CashFlow.objects.filter(account=self.mf).count(), 2)

    def test_recategorising_confirmed_investment_removes_flow(self):
        self.imp(self.bank, "hdfc.csv", HDFC_CSV)
        t = Transaction.objects.get(description__contains="ZERODHA")
        matching.confirm(t)
        t.category, t.investment_account = "transfer", None
        matching.confirm(t)
        self.assertFalse(CashFlow.objects.exists())


class FakeStream:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, model="m",
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))


class FakeClaude:
    """Returns queued payloads; records which model each call used."""

    def __init__(self, *payloads):
        self.payloads, self.models = list(payloads), []
        outer = self

        class M:
            def stream(self, **kw):
                outer.models.append(kw["model"])
                return FakeStream(outer.payloads.pop(0))

        self.messages = M()
        self.beta = SimpleNamespace(messages=M())


def extraction(total_debits):
    return {"statement_type": "card", "period_start": "2026-10-01", "period_end": "2026-10-31",
            "opening_balance": 0, "closing_balance": 0, "total_debits": total_debits, "total_credits": 0,
            "transactions": [
                {"date": "2026-10-04", "description": "BLUE TOKAI COFFEE", "amount": 450, "direction": "debit",
                 "balance": 0, "category": "expense", "spend_category": "dining"},
                {"date": "2026-10-05", "description": "SOME NEW SHOP", "amount": 1550, "direction": "debit",
                 "balance": 0, "category": "expense", "spend_category": "shopping"},
            ]}


def blank_pdf(password=None):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(200, 200)
    if password:
        w.encrypt(password)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class PdfTests(Setup):
    def test_pdf_read_by_claude_with_retry_on_mismatch(self):
        client = FakeClaude(extraction(9999), extraction(2000))  # first read doesn't reconcile
        stmt = importer.import_statement(self.card, "card.pdf", blank_pdf("secret"), "secret", client=client)
        self.assertEqual(client.models, ["claude-haiku-5-5", "claude-sonnet-5-5"])
        self.assertEqual((stmt.added, stmt.parser, stmt.warning), (2, "claude", ""))
        t = stmt.transactions.get(description="BLUE TOKAI COFFEE")
        self.assertEqual((t.amount, t.category, t.spend_category, t.source), (D("-450"), "expense", "dining", "ai"))
        self.assertEqual(stmt.period_start, dt.date(2026, 10, 1))

    def test_wrong_pdf_password(self):
        with self.assertRaises(ValueError):
            parse_ai.decrypt_pdf(blank_pdf("secret"), "nope")

    def test_uncategorised_rows_go_to_claude(self):
        client = FakeClaude({"items": [{"i": 0, "category": "expense", "spend_category": "household"}]})
        stmt = importer.import_statement(self.bank, "x.csv",
                                         b"Date,Narration,Debit,Credit\n01/10/26,RAMESH HOUSEHELP,8000,\n", client=client)
        t = stmt.transactions.get()
        self.assertEqual((t.category, t.spend_category, t.source), ("expense", "household", "ai"))


class SpendingTests(Setup):
    def test_monthly_spending_and_adopt(self):
        self.imp(self.bank, "hdfc.csv", HDFC_CSV)
        self.imp(self.card, "card.csv", CARD_CSV)
        for st in StatementUpload.objects.all():  # both statements cover all of October
            st.period_start, st.period_end = dt.date(2026, 10, 1), dt.date(2026, 10, 31)
            st.save()
        rows = spending.monthly()
        oct_ = rows[-1]
        # bank: swiggy 650 + LIC 12000; card: zomato 1200 + amazon 3500 - refund 500
        self.assertEqual(oct_["spend"], D("16850"))
        self.assertEqual(oct_["emi"], D("70000"))
        self.assertTrue(oct_["complete"])
        MonthlyExpense.objects.create(month=dt.date(2026, 1, 1), amount=200000, includes_emi=True)
        s = spending.suggestion(rows)
        self.assertEqual(s["amount"], D("86850"))
        spending.adopt(s["amount"], s["include_emi"])
        self.assertEqual(MonthlyExpense.objects.order_by("-month").first().amount, D("86850"))

    def test_incomplete_month_flagged(self):
        self.imp(self.bank, "hdfc.csv", HDFC_CSV)  # covers 1-31 Oct from dates
        self.imp(self.card, "c.csv", "Transaction Date,Details,Amount (INR)\n20/10/2026,UBER,300\n")
        rows = spending.monthly()
        self.assertFalse(rows[-1]["complete"])
        self.assertEqual([a.name for a in rows[-1]["missing"]], ["HDFC Regalia"])
        self.assertIsNone(spending.suggestion(rows))


class PageTests(Setup):
    def setUp(self):
        super().setUp()
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")

    def test_review_search_and_sort(self):
        st = StatementUpload.objects.create(account=self.bank, filename="t.csv", sha256="x", parser="table")
        mk = lambda d, desc, amt: Transaction.objects.create(
            statement=st, account=self.bank, date=dt.date(2026, 10, d), description=desc, amount=D(amt),
            dedupe_key=f"k{d}{desc}")
        mk(1, "SWIGGY ORDER", "-450"), mk(2, "AMAZON PAY", "-1200"), mk(3, "SALARY ACME", "250000")
        page = self.client.get("/statements/review/?status=all&q=swiggy")
        self.assertContains(page, "SWIGGY ORDER")
        self.assertNotContains(page, "AMAZON PAY")
        self.assertContains(self.client.get("/statements/review/?status=all&q=1,200"), "AMAZON PAY")  # amount match
        page = self.client.get("/statements/review/?status=all&sort=amount")
        self.assertEqual([t.description for t in page.context["page"]], ["AMAZON PAY", "SWIGGY ORDER", "SALARY ACME"])
        self.assertIn("sort=-amount", page.context["sort_links"]["amount"]["url"])  # clicking again flips it
        self.assertEqual(self.client.get("/statements/review/?sort=bogus").context["sort"], "-date")

    def test_tables_are_sortable_and_filterable(self):
        for url in ("/accounts/", "/money-moved/", "/statements/"):
            page = self.client.get(url).content.decode()
            self.assertIn("data-sort", page, url)
            self.assertIn("data-filter", page, url)

    def test_upload_review_remember_confirm(self):
        f = SimpleUploadedFile("hdfc.csv", HDFC_CSV.replace("SWIGGY-SWIGGY@ICICI", "BLUETOKAI-BT@ICICI").encode())
        from unittest import mock
        from statements import batch as batches
        batches.READERS = 1
        with mock.patch.object(batches, "start_worker"):
            r = self.client.post("/statements/upload/", {"account": self.bank.id, "files": [f]})
        from core.models import UploadBatch
        b = UploadBatch.objects.get()
        self.assertRedirects(r, f"/statements/batches/{b.pk}/")
        batches.process(b)  # no API key in tests: unknown merchants stay uncategorised
        page = self.client.get("/statements/review/")
        self.assertContains(page, "BLUETOKAI")
        t = Transaction.objects.get(description__contains="BLUETOKAI")
        # BLUETOKAI isn't a known merchant: with Claude unavailable it's left for us to tag
        self.assertEqual(t.category, "")
        post = {"txn": [t.id], f"cat_{t.id}": "expense", f"spend_{t.id}": "dining",
                f"rem_{t.id}": "on", f"kw_{t.id}": "BLUETOKAI", f"ok_{t.id}": "on"}
        self.client.post("/statements/review/save/", post)
        t.refresh_from_db()
        self.assertEqual((t.category, t.spend_category, t.confirmed, t.source), ("expense", "dining", True, "user"))
        self.assertTrue(CategoryRule.objects.filter(keyword="BLUETOKAI", is_builtin=False).exists())
        self.assertContains(self.client.get("/statements/"), "BLUETOKAI")
        self.assertEqual(self.client.get("/spending/").status_code, 200)

    def test_accounts_page_lists_and_edits_inline(self):
        page = self.client.get("/accounts/")
        self.assertContains(page, "HDFC Regalia")
        self.assertContains(page, f'id="f-{self.bank.id}"')
        r = self.client.post(f"/accounts/{self.bank.id}/", {
            "name": "HDFC Salary", "institution": "HDFC Bank", "match_hints": "50100123", "is_active": "on"})
        self.assertRedirects(r, f"/accounts/#acc-{self.bank.id}", fetch_redirect_response=False)
        self.bank.refresh_from_db()
        self.assertEqual((self.bank.name, self.bank.institution, self.bank.match_hints, self.bank.is_active),
                         ("HDFC Salary", "HDFC Bank", "50100123", True))
        # same name as another of Saurabh's accounts (any case): refused, nothing changes
        self.client.post(f"/accounts/{self.card.id}/", {"name": "hdfc salary", "institution": "X", "is_active": "on"})
        self.card.refresh_from_db()
        self.assertEqual((self.card.name, self.card.institution), ("HDFC Regalia", ""))
        # Pritika may reuse it; unticking Active archives the account
        self.client.post(f"/accounts/{self.pbank.id}/", {"name": "HDFC Salary", "institution": "ICICI"})
        self.pbank.refresh_from_db()
        self.assertEqual((self.pbank.name, self.pbank.is_active), ("HDFC Salary", False))

    def test_add_account(self):
        r = self.client.post("/accounts/add/", {"owner": self.p.id, "kind": "card", "name": "SC Smart",
                                                "institution": "Standard Chartered", "match_hints": "1234"})
        acc = Account.objects.get(name="SC Smart")
        self.assertRedirects(r, f"/accounts/#acc-{acc.id}", fetch_redirect_response=False)
        self.assertEqual((acc.owner, acc.asset_class, acc.match_hints), (self.p, "liability", "1234"))
        r = self.client.post("/accounts/add/", {"owner": self.s.id, "kind": "loan", "name": "Car loan"})
        loan = Account.objects.get(name="Car loan")
        self.assertRedirects(r, f"/loans/new/?account={loan.id}", fetch_redirect_response=False)
        self.client.post("/accounts/add/", {"owner": self.s.id, "kind": "bank", "name": "Bank HDFC"})  # duplicate
        self.assertEqual(Account.objects.filter(owner=self.s, name="Bank HDFC").count(), 1)

    def test_month_end_form_prefills_bank_balance(self):
        from core.views import _values_from_pulls
        self.imp(self.bank, "hdfc.csv", HDFC_CSV)
        vals = _values_from_pulls([self.bank], dt.date(2026, 11, 2))
        self.assertEqual(vals[self.bank.id], D("502350.00"))
        self.assertEqual(_values_from_pulls([self.bank], dt.date(2026, 12, 31)), {})  # too old


class ZerodhaSettlementTests(Setup):
    def test_quarterly_settlement_counted_once(self):
        stmt = self.imp(self.bank, "s.csv", "Date,Narration,Debit,Credit\n"
                        "17/10/26,NEFT-YESF362-ZERODHA BROKING LTD-DSCNB A/C,,13264.88\n"
                        "20/10/26,UPI/ZERODHA BR/XX.brk@validic/123,25000,\n")
        payout, topup = stmt.transactions.order_by("date")
        self.assertEqual((payout.category, payout.investment_account), ("inv_return", self.zer))
        self.assertEqual((topup.category, topup.investment_account), ("investment", self.zer))
        matching.confirm(payout)
        matching.confirm(topup)
        ledger = ("particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance\n"
                  "Funds transferred back as part of quarterly settlement,2026-10-16,NSE-EQ,Bank Payments,13264.88,0,0\n"
                  "Funds added using UPI,2026-10-20,NSE-EQ,Bank Receipts,0,25000,25000\n")
        zerodha_ledger.import_ledger(self.zer, ledger)
        flows = sorted(CashFlow.objects.filter(account=self.zer).values_list("source", "amount"))
        self.assertEqual(flows, [("kite", D("-13264.88")), ("kite", D("25000"))])  # net invested 11,735.12


# ------------------------------------------------------------------ bulk uploads
import tempfile  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from django.core.management import call_command  # noqa: E402
from django.test import override_settings  # noqa: E402

from core.models import UploadBatch, UploadItem  # noqa: E402

from . import batch as batches  # noqa: E402
from . import detect  # noqa: E402

BANK_WITH_NUMBER = "Account No :11112222333344\n" + HDFC_CSV.split("\n", 3)[3]
CARD_WITH_NUMBER = "Card No: 4111 11XX XXXX 5150\n" + CARD_CSV


class DetectTests(Setup):
    def setUp(self):
        super().setUp()
        self.bank.match_hints = "11112222333344"
        self.bank.save()
        self.card.match_hints = "5150"
        self.card.save()

    def test_detects_by_number_and_prefers_longest(self):
        acc, why = detect.detect("b.csv", (BANK_WITH_NUMBER + "01/10/26,CC 0004111XXXXXX5150 AUTOPAY,1,,1,\n").encode())
        self.assertEqual(acc, self.bank)  # the card number in a narration doesn't win
        self.assertEqual(detect.detect("c.csv", CARD_WITH_NUMBER.encode())[0], self.card)

    def test_amounts_dont_match_card_digits(self):
        acc, why = detect.detect("x.csv", b"Date,Details,Amount\n01/10/2026,SHOP,15150.00\n02/10/2026,SHOP,5150.50\n")
        self.assertIsNone(acc)

    def test_ambiguous(self):
        self.pbank.match_hints = "5150"
        self.pbank.save()
        acc, why = detect.detect("c.csv", CARD_WITH_NUMBER.encode())
        self.assertIsNone(acc)
        self.assertIn("more than one", why)


@override_settings(BASE_DIR=Path(tempfile.mkdtemp()))
class BatchTests(Setup):
    def setUp(self):
        super().setUp()
        batches.UPLOAD_ROOT = Path(tempfile.mkdtemp())
        batches.READERS = 1  # the in-memory test database can't be shared across threads
        self.bank.match_hints, self.card.match_hints = "11112222333344", "5150"
        self.bank.save()
        self.card.save()

    def zip_of(self, files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, text in files:
                z.writestr(name, text)
        return buf.getvalue()

    def test_zip_batch_detects_imports_and_flags_unknown(self):
        z = self.zip_of([("hdfc_oct.csv", BANK_WITH_NUMBER), ("card_oct.csv", CARD_WITH_NUMBER),
                         ("mystery.csv", "Date,Narration,Debit,Credit\n01/10/26,X,1,\n"), ("__MACOSX/._junk.csv", "x")])
        b = batches.create_batch([("statements.zip", z)], passwords="secret")
        self.assertEqual(b.items.count(), 3)
        self.assertEqual(b.items.filter(status="needs_account").count(), 1)
        batches.process(b, client=NoClaude())
        b.refresh_from_db()
        self.assertEqual(dict(b.counts()), {"done": 2, "needs_account": 1})
        self.assertEqual(b.status, "waiting")
        self.assertEqual(b.passwords, "secret")  # kept until every file is dealt with
        self.assertEqual(Transaction.objects.filter(account=self.bank).count(), 8)
        # card payment pairs across files in the same batch
        self.assertTrue(Transaction.objects.filter(category="card_payment", matched_with__isnull=False).exists())
        # imported files are deleted from disk
        self.assertFalse(any(Path(i.path).exists() for i in b.items.filter(status="done")))

    def test_assign_account_via_page_remembers_number(self):
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")
        b = batches.create_batch([("unknown.csv", b"Acct 99887766\nDate,Narration,Debit,Credit\n01/10/26,X,1,\n")])
        item = b.items.get()
        with mock.patch.object(batches, "start_worker") as start:
            self.client.post(f"/statements/batches/{b.pk}/assign/", {
                f"account_{item.id}": self.pbank.id, f"remember_{item.id}": "on", f"hint_{item.id}": "99887766"})
            start.assert_called_once()
        batches.process(b, client=FakeClaude({"items": []}))
        b.refresh_from_db()
        self.assertEqual((b.status, b.passwords), ("done", ""))
        self.pbank.refresh_from_db()
        self.assertEqual(self.pbank.match_hints, "99887766")
        self.assertContains(self.client.get(f"/statements/batches/{b.pk}/"), "Imported")

    def test_duplicate_file_and_forced_account(self):
        self.imp(self.bank, "a.csv", HDFC_CSV)
        b = batches.create_batch([("a.csv", HDFC_CSV.encode()), ("b.csv", CARD_CSV.encode())], account=self.card)
        self.assertEqual(sorted(b.items.values_list("status", flat=True)), ["duplicate", "queued"])
        self.assertEqual(b.items.get(status="queued").account, self.card)

    def test_pdf_passwords_tried_in_turn(self):
        b = batches.create_batch([("amex.pdf", blank_pdf("pw2"))], passwords="pw1\npw2", account=self.card)
        batches.process(b, client=FakeClaude(extraction(2000)))
        item = b.items.get()
        self.assertEqual(item.status, "done", item.message)

    def test_upload_view_starts_worker(self):
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")
        f = SimpleUploadedFile("hdfc.csv", BANK_WITH_NUMBER.encode())
        with mock.patch.object(batches, "start_worker") as start:
            r = self.client.post("/statements/upload/", {"files": [f]})
        b = UploadBatch.objects.get()
        self.assertRedirects(r, f"/statements/batches/{b.pk}/")
        start.assert_called_once()
        self.assertEqual(b.items.get().account, self.bank)

    def test_import_folder_command(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "hdfc.csv").write_text(BANK_WITH_NUMBER)
        (folder / "notes.txt").write_text("ignore me")
        out = io.StringIO()
        with mock.patch("statements.importer.parse_ai.categorize", return_value={}):
            call_command("import_folder", str(folder), stdout=out)
        self.assertIn("Imported", out.getvalue())
        self.assertEqual(UploadItem.objects.get().status, "done")


class SpendingMonthTests(Setup):
    CARD = ("Transaction Date,Details,Amount (INR)\n"
            "03/09/2026,SWIGGY ORDER,500.00\n"
            "05/09/2026,SWIGGY ORDER,700.00\n"
            "07/09/2026,AMAZON PAY INDIA,\"2,000.00\"\n"
            "02/10/2026,SWIGGY ORDER,900.00\n")

    def setUp(self):
        super().setUp()
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")
        self.imp(self.card, "card.csv", self.CARD)

    def test_groups_match_the_month_total_biggest_first(self):
        d = spending.month_detail(dt.date(2026, 9, 1))
        self.assertEqual([(g["key"], g["total"], g["count"]) for g in d["groups"]],
                         [("shopping", D("2000"), 1), ("dining", D("1200"), 2)])
        row = next(r for r in spending.monthly() if r["month"] == dt.date(2026, 9, 1))
        self.assertEqual(d["spend"], row["spend"])
        self.assertEqual([t.amount for t in d["groups"][1]["rows"]], [D("-700"), D("-500")])  # largest first

    def test_page_lets_you_recategorise_and_confirm_from_the_month(self):
        url = "/spending/2026/9/"
        page = self.client.get(url)
        self.assertContains(page, "SWIGGY ORDER", count=4)  # title attribute + text for the 2 September rows
        self.assertNotContains(page, 'class="ok" checked')  # nothing is pre-ticked here
        t = Transaction.objects.get(amount=D("-500"))
        other = Transaction.objects.get(amount=D("-700"))
        saved = self.client.post("/statements/review/save/", {
            "next": url, "txn": [t.id, other.id], f"cat_{t.id}": "expense", f"spend_{t.id}": "groceries",
            f"ok_{t.id}": "on", f"cat_{other.id}": "expense", f"spend_{other.id}": "dining"})
        self.assertRedirects(saved, url)
        t.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((t.spend_category, t.confirmed), ("groceries", True))
        self.assertFalse(other.confirmed)
        self.assertEqual(self.client.get(url + "?show=unconfirmed").context["groups"][0]["total"], D("2000"))

    def test_unconfirmed_filter_hides_rows_but_keeps_totals(self):
        matching.confirm(Transaction.objects.get(amount=D("-2000")))
        d = spending.month_detail(dt.date(2026, 9, 1), unconfirmed_only=True)
        self.assertEqual((d["spend"], d["unconfirmed"], d["groups"][0]["rows"]), (D("3200"), 2, []))

    def test_home_costs_are_shown_apart_from_spending(self):
        t = Transaction.objects.get(amount=D("-2000"))
        t.category, t.spend_category = "home", ""
        t.save()
        row = next(r for r in spending.monthly() if r["month"] == dt.date(2026, 9, 1))
        self.assertEqual((row["spend"], row["home"]), (D("1200"), D("2000")))
        self.assertEqual(sum(row["by_cat"].values()), D("1200"))
        d = spending.month_detail(dt.date(2026, 9, 1))
        self.assertEqual((d["spend"], d["home"], d["groups"][-1]["key"]), (D("1200"), D("2000"), "home"))
        self.assertEqual(spending.home_total(), D("2000"))
        self.assertContains(self.client.get("/spending/"), "one-off")

    def statement(self, start, end):
        StatementUpload.objects.create(account=self.card, filename=f"{start}.csv", sha256=str(start), parser="table",
                                       period_start=start, period_end=end)

    def test_coverage_skips_months_before_the_account_existed(self):
        StatementUpload.objects.filter(account=self.card).update(period_start=dt.date(2026, 8, 9), period_end=dt.date(2026, 8, 31))
        months = [dt.date(2026, 7, 1), dt.date(2026, 8, 1)]
        self.assertEqual(spending.coverage(months)[months[1]], [self.card])  # started mid-month, nobody said so
        self.card.statements_from = dt.date(2026, 8, 1)
        self.card.save()
        cov = spending.coverage(months)
        self.assertEqual((cov[months[0]], cov[months[1]]), ([], []))  # first month only counts from its first statement

    def test_coverage_allows_a_few_days_at_month_end(self):
        StatementUpload.objects.filter(account=self.card).update(period_start=dt.date(2026, 9, 3), period_end=dt.date(2026, 9, 30))
        self.assertEqual(spending.coverage([dt.date(2026, 9, 1)])[dt.date(2026, 9, 1)], [])
        StatementUpload.objects.filter(account=self.card).update(period_end=dt.date(2026, 9, 20))
        self.assertEqual(spending.coverage([dt.date(2026, 9, 1)])[dt.date(2026, 9, 1)], [self.card])

    def test_didnt_have_it_yet_button_goes_back_to_spending(self):
        StatementUpload.objects.filter(account=self.card).update(period_start=dt.date(2026, 10, 1), period_end=dt.date(2026, 10, 31))
        self.assertEqual(self.client.get("/spending/").content.decode().count("have it yet"), 1)  # Sep only, not Oct's gap
        r = self.client.post(f"/checklist/from/{self.card.id}/", {"statements_from": "2026-10-01", "back": "spending"})
        self.assertRedirects(r, "/spending/")
        self.assertNotContains(self.client.get("/spending/"), "have it yet")

    def test_month_neighbours_and_bad_month(self):
        self.assertEqual(spending.month_neighbours(dt.date(2026, 9, 1)), (None, dt.date(2026, 10, 1)))
        self.assertEqual(self.client.get("/spending/2026/13/").status_code, 404)

    def test_a_full_month_saves_in_one_post(self):
        stmt = StatementUpload.objects.first()
        Transaction.objects.bulk_create(
            Transaction(statement=stmt, account=self.card, date=dt.date(2026, 9, 9), description=f"ROW {i}",
                        amount=D("-1"), category="expense", spend_category="other", dedupe_key=f"bulk{i}")
            for i in range(300))
        ids = list(Transaction.objects.filter(description__startswith="ROW").values_list("id", flat=True))
        data = {"txn": ids}
        for i in ids:
            data |= {f"cat_{i}": "expense", f"spend_{i}": "other", f"party_{i}": "", f"inv_{i}": "", f"kw_{i}": "ROW"}
        self.assertEqual(self.client.post("/statements/review/save/", data).status_code, 302)


class ReimbursementTests(Setup):
    BANK = ("Date,Narration,Debit,Credit\n"
            "10/09/26,UPI/ACMECO/XX.ibz@icici/AmazonReim/I,,4321.09\n"
            "12/09/26,UPI/ACMECO/XX.ibz@icici/AmazonReim/I,,777.00\n")
    CARD = ("Transaction Date,Details,Amount (INR)\n"
            "28/08/2026,AMAZON PAY INDIA,\"4,321.09\"\n"
            "30/08/2026,AMAZON PAY INDIA,\"2,000.00\"\n")

    def setUp(self):
        super().setUp()
        CategoryRule.objects.create(keyword="AMAZONREIM", applies_to="bank", direction="credit",
                                    category="reimbursement", party="Acme")

    def test_pairs_purchase_and_reimbursement_and_tracks_owed(self):
        self.imp(self.card, "card.csv", self.CARD)
        self.imp(self.bank, "bank.csv", self.BANK)
        paid = Transaction.objects.get(account=self.card, amount=D("-4321.09"))
        back = Transaction.objects.get(account=self.bank, amount=D("4321.09"))
        self.assertEqual((back.category, back.party), ("reimbursement", "Acme"))
        self.assertEqual((paid.category, paid.party, paid.matched_with), ("reimbursable", "Acme", back))
        other = Transaction.objects.get(account=self.card, amount=D("-2000"))
        self.assertEqual(other.category, "expense")  # personal Amazon order untouched
        s = {r["party"]: r for r in reimburse.summary()}["Acme"]
        self.assertEqual((s["spent"], s["received"], s["owed"]), (D("4321.09"), D("5098.09"), D("-777.00")))
        self.assertEqual(len(s["unmatched_received"]), 1)  # the 777 purchase isn't in these statements

    def test_not_counted_as_spending(self):
        self.imp(self.card, "card.csv", self.CARD)
        self.imp(self.bank, "bank.csv", self.BANK)
        aug = next(r for r in spending.monthly() if r["month"] == dt.date(2026, 8, 1))
        self.assertEqual(aug["spend"], D("2000"))  # only the personal order
        sep = next((r for r in spending.monthly() if r["month"] == dt.date(2026, 9, 1)), None)
        self.assertTrue(sep is None or sep["spend"] == 0)

    def test_purchase_older_than_60_days_not_paired(self):
        self.imp(self.card, "c.csv", "Transaction Date,Details,Amount (INR)\n01/06/2026,AMAZON PAY INDIA,\"4,321.09\"\n")
        self.imp(self.bank, "bank.csv", self.BANK)
        self.assertEqual(Transaction.objects.get(account=self.card).category, "expense")

    def test_same_day_reimbursement_is_not_a_card_payment(self):
        card = "Transaction Date,Details,Amount (INR)\n10/09/2026,AMAZON PAY INDIA,\"4,321.09\"\n"
        for order in ((self.card, "c.csv", card), (self.bank, "b.csv", self.BANK)), \
                     ((self.bank, "b.csv", self.BANK), (self.card, "c.csv", card)):
            Transaction.objects.all().delete()
            StatementUpload.objects.all().delete()
            for args in order:
                self.imp(*args)
            paid = Transaction.objects.get(account=self.card)
            back = Transaction.objects.get(account=self.bank, amount=D("4321.09"))
            self.assertEqual((paid.category, back.category), ("reimbursable", "reimbursement"))
            self.assertEqual(paid.matched_with, back)

    def test_card_purchase_never_pairs_with_an_unlabelled_credit(self):
        CategoryRule.objects.create(keyword="UPI PAY", applies_to="bank", direction="credit", category="income")
        self.imp(self.bank, "b.csv", "Date,Narration,Debit,Credit\n10/09/26,UPI/ACMECO/XX.ibz@icici/UPI Pay/I,,500.00\n")
        self.imp(self.card, "c.csv", "Transaction Date,Details,Amount (INR)\n10/09/2026,AMAZON PAY INDIA,500.00\n")
        paid = Transaction.objects.get(account=self.card)
        self.assertEqual((paid.category, paid.matched_with), ("expense", None))

    def test_excess_payment_refunded_from_card_to_bank_still_pairs(self):
        CategoryRule.objects.create(keyword="EXC PY", applies_to="any", direction="any", category="transfer")
        self.imp(self.bank, "b.csv", "Date,Narration,Debit,Credit\n22/07/26,EXC PYMT - 1234 XXXX XXXX 5678,,900.50\n")
        self.imp(self.card, "c.csv", "Transaction Date,Details,Amount (INR)\n22/07/2026,EXC PYT REFUND TO AC 1 234,900.50\n")
        out = Transaction.objects.get(account=self.card)
        self.assertEqual(out.category, "card_payment")
        self.assertEqual(out.matched_with.account, self.bank)

    def test_card_can_post_a_day_after_the_reimbursement(self):
        self.imp(self.bank, "bank.csv", self.BANK)
        self.imp(self.card, "c.csv", "Transaction Date,Details,Amount (INR)\n11/09/2026,AMAZON PAY INDIA,\"4,321.09\"\n")
        self.assertEqual(Transaction.objects.get(account=self.card).category, "reimbursable")

    def test_marking_reimbursable_by_hand_on_review_page(self):
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")
        self.imp(self.card, "card.csv", self.CARD)
        t = Transaction.objects.get(amount=D("-2000"))
        self.client.post("/statements/review/save/", {"txn": [t.id], f"cat_{t.id}": "reimbursable",
                                                      f"party_{t.id}": "Acme"})
        t.refresh_from_db()
        self.assertEqual((t.category, t.party), ("reimbursable", "Acme"))
        self.assertContains(self.client.get("/spending/"), "Still owed")
