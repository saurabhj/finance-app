"""Claude reads PDF statements (any bank/card layout) and guesses categories."""
import base64
import io
import json
from decimal import Decimal

from django.conf import settings

from ai.review import ReviewError, call_claude
from core.models import Category, SpendCategory

CATS = [c for c in Category.values]
SPENDS = [""] + list(SpendCategory.values)

GUIDE = """Categories:
- investment: money leaving a bank account into an investment platform (Zerodha, ET Money, ICCL/BSE StarMF, FD booking).
- inv_return: money coming back from those platforms (payouts, redemptions, FD maturity).
- transfer: between the family's own accounts (names: {names}), including self transfers.
- card_payment: paying a credit-card bill from a bank (CRED, card bill) or, on a card statement, the payment received.
- loan_emi: home-loan EMI. income: salary, interest, dividends. refund: refunds/cashback/reversals.
- expense: any other spending; also set spend_category. ignore: anything that isn't money moving (e.g. 'opening balance').
Smallcase / Weekend Investing subscription fees are expense (subscriptions), not investment."""

EXTRACT_SYSTEM = """You transcribe Indian bank and credit-card statements into structured data with complete accuracy.
Rules:
- Include EVERY transaction row, in order; never summarise, merge or skip. Join descriptions that wrap onto several lines.
- Amounts are INR, always positive numbers; use direction = debit (money out of the account / card spend) or credit (money in / card payment, refund).
- Dates as YYYY-MM-DD. Indian statements use day-first dates.
- Copy opening/closing balance and total debits/credits exactly as printed (0 if the statement doesn't show them).
- Do not include opening balance, closing balance, reward-point or summary rows as transactions.
""" + GUIDE

TXN_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "description": {"type": "string"},
        "amount": {"type": "number"},
        "direction": {"type": "string", "enum": ["debit", "credit"]},
        "balance": {"type": "number", "description": "Running balance after this row, 0 if not shown"},
        "category": {"type": "string", "enum": CATS},
        "spend_category": {"type": "string", "enum": SPENDS},
    },
    "required": ["date", "description", "amount", "direction", "balance", "category", "spend_category"],
    "additionalProperties": False,
}
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "statement_type": {"type": "string", "enum": ["bank", "card"]},
        "period_start": {"type": "string"},
        "period_end": {"type": "string"},
        "opening_balance": {"type": "number"},
        "closing_balance": {"type": "number"},
        "total_debits": {"type": "number"},
        "total_credits": {"type": "number"},
        "transactions": {"type": "array", "items": TXN_SCHEMA},
    },
    "required": ["statement_type", "period_start", "period_end", "opening_balance", "closing_balance",
                 "total_debits", "total_credits", "transactions"],
    "additionalProperties": False,
}
CATEGORIZE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "category": {"type": "string", "enum": CATS},
                       "spend_category": {"type": "string", "enum": SPENDS}},
        "required": ["i", "category", "spend_category"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def decrypt_pdf(data, password):
    """Return unencrypted PDF bytes (pypdf); raises ValueError on a wrong password."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    if not reader.is_encrypted:
        return data
    if not reader.decrypt(password or ""):
        raise ValueError("Wrong PDF password.")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def check_totals(result):
    """Return a warning string if the transcription doesn't reconcile with the printed totals."""
    txns = result["transactions"]
    debits = sum(Decimal(str(t["amount"])) for t in txns if t["direction"] == "debit")
    credits = sum(Decimal(str(t["amount"])) for t in txns if t["direction"] == "credit")
    problems = []
    if result["total_debits"] and abs(debits - Decimal(str(result["total_debits"]))) > 1:
        problems.append(f"debits add up to ₹{debits:,.2f} but the statement says ₹{result['total_debits']:,.2f}")
    if result["total_credits"] and abs(credits - Decimal(str(result["total_credits"]))) > 1:
        problems.append(f"credits add up to ₹{credits:,.2f} but the statement says ₹{result['total_credits']:,.2f}")
    if result["statement_type"] == "bank" and result["opening_balance"] and result["closing_balance"]:
        expected = Decimal(str(result["opening_balance"])) + credits - debits
        if abs(expected - Decimal(str(result["closing_balance"]))) > 1:
            problems.append(f"opening + credits − debits = ₹{expected:,.2f}, closing shows ₹{result['closing_balance']:,.2f}")
    return "; ".join(problems)


def extract(content_blocks, names, client=None):
    """Transcribe with the cheap model; if totals don't reconcile, re-read once with a stronger one."""
    system = EXTRACT_SYSTEM.format(names=", ".join(names))
    best, warning = None, ""
    for model in (settings.AI_MODEL_EXTRACT, settings.AI_MODEL_LIGHT):
        result, _usage, _served = call_claude(model, content_blocks, EXTRACT_SCHEMA, "medium",
                                              client=client, system=system)
        warning = check_totals(result)
        best = result
        if not warning:
            break
    return best, warning


def extract_pdf(data, password, names, client=None):
    pdf = decrypt_pdf(data, password)
    blocks = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                        "data": base64.standard_b64encode(pdf).decode()}},
        {"type": "text", "text": "Transcribe every transaction in this statement."},
    ]
    return extract(blocks, names, client)


def extract_text(text, names, client=None):
    blocks = [{"type": "text", "text": f"Transcribe every transaction in this statement export:\n\n{text[:400000]}"}]
    return extract(blocks, names, client)


def categorize(txns, account_kind, names, client=None):
    """Guess categories for uncategorised transactions. txns: list of Transaction (unsaved ok)."""
    out = {}
    system = ("You categorise Indian bank and credit-card transactions for a family's finance app.\n"
              + GUIDE.format(names=", ".join(names)))
    for start in range(0, len(txns), 200):
        chunk = txns[start:start + 200]
        lines = [{"i": start + k, "description": t.description, "amount": float(abs(t.amount)),
                  "direction": "credit" if t.amount > 0 else "debit"} for k, t in enumerate(chunk)]
        prompt = f"Account type: {account_kind}. Categorise each line:\n{json.dumps(lines)}"
        try:
            result, _, _ = call_claude(settings.AI_MODEL_EXTRACT, prompt, CATEGORIZE_SCHEMA, "low",
                                       client=client, system=system, max_tokens=32000)
        except ReviewError:
            continue  # leave them uncategorised for you to tag
        for item in result["items"]:
            out[item["i"]] = (item["category"], item["spend_category"])
    return out
