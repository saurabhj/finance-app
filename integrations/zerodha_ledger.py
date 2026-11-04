"""Import money added to / withdrawn from Zerodha from the Console ledger CSV.

Kite Connect has no API for fund transfers, so download the ledger from
console.zerodha.com -> Funds -> Statement -> Ledger (segment: Equity) -> CSV
and upload it. Only bank receipts (funds added) and bank payments (payouts)
become cash flows; trades, charges and dividends stay inside the account and
show up as market gain/loss. Re-uploading overlapping ranges is safe.
"""
import csv
import datetime as dt
import hashlib
import io
from decimal import Decimal, InvalidOperation

from core.models import CashFlow

PAYIN_HINTS = ("funds added", "fund added", "payin", "pay-in", "pay in")
PAYOUT_HINTS = ("payout", "pay-out", "funds withdrawn", "withdrawal")


def _num(v):
    try:
        return Decimal(str(v).replace(",", "").strip() or "0")
    except InvalidOperation:
        return Decimal("0")


def _date(v):
    v = v.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {v!r}")


def classify_row(row):
    """Return +amount for a pay-in, -amount for a payout, None otherwise."""
    particulars = (row.get("particulars") or "").lower()
    voucher = (row.get("voucher_type") or "").lower()
    debit, credit = _num(row.get("debit")), _num(row.get("credit"))
    if "bank receipt" in voucher or any(h in particulars for h in PAYIN_HINTS):
        return credit or None
    if "bank payment" in voucher or any(h in particulars for h in PAYOUT_HINTS):
        return -debit if debit else None
    return None


def parse(text):
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    reader.fieldnames = [(f or "").strip().lower().replace(" ", "_") for f in reader.fieldnames or []]
    for row in reader:
        if not row.get("posting_date"):
            continue
        amount = classify_row(row)
        if amount:
            yield _date(row["posting_date"]), amount, (row.get("particulars") or "").strip()


def import_ledger(account, text):
    created = skipped = 0
    for date, amount, particulars in parse(text):
        key = hashlib.sha1(f"{account.id}|{date}|{amount}|{particulars}".encode()).hexdigest()[:24]
        _, made = CashFlow.objects.get_or_create(
            external_id=f"kite:{key}",
            defaults=dict(
                date=date, account=account, amount=amount, source=CashFlow.Source.KITE,
                kind=CashFlow.Kind.INVEST if amount > 0 else CashFlow.Kind.WITHDRAW,
                note=particulars[:200],
            ),
        )
        created += made
        skipped += not made
    return created, skipped
