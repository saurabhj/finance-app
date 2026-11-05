"""Pairs transfers between our own accounts, links bank investment payments to
Zerodha-ledger / CAS flows so nothing is counted twice, and confirms transactions."""
import datetime as dt
from decimal import Decimal

from django.db import transaction as db_tx

from core.models import Account, CashFlow, Category, Transaction

PAIRABLE = ("", Category.EXPENSE, Category.INCOME, Category.TRANSFER, Category.REFUND)


def _tolerance(amount):
    """₹2 or 0.01% (CAS purchase amounts exclude stamp duty, so they're a few rupees lower)."""
    return max(Decimal("2"), abs(amount) * Decimal("0.0001"))


def _money_can_move(t, other):
    """Money leaves a card only when the bank refunds an overpayment. A purchase and a same-amount
    credit elsewhere are someone paying you back (or a coincidence), not a transfer."""
    out = t if t.amount < 0 else other
    return out.account.kind != Account.Kind.CARD or "REFUND" in out.description.upper()


def pair_transfers(txns):
    """Mark same-amount, opposite-direction pairs between family accounts as transfers / card payments."""
    changed = 0
    pairable = PAIRABLE + (Category.CARD_PAYMENT,)
    for t in txns:
        if t.confirmed or t.matched_with_id or t.category not in pairable:
            continue
        window = (t.date - dt.timedelta(days=3), t.date + dt.timedelta(days=5 if t.account.kind == "card" else 3))
        candidates = (Transaction.objects.filter(amount=-t.amount, date__range=window, matched_with__isnull=True,
                                                 category__in=pairable)
                      .exclude(account=t.account).exclude(pk=t.pk)
                      .exclude(account__kind=Account.Kind.LOAN)
                      .select_related("account"))
        other = next((c for c in candidates if _money_can_move(t, c)), None)
        if not other:
            continue
        card_side = "card" in (t.account.kind, other.account.kind)
        cat = Category.CARD_PAYMENT if card_side else Category.TRANSFER
        for a, b in ((t, other), (other, t)):
            if not a.confirmed:
                a.category, a.spend_category, a.source = cat, "", Transaction.Source.MATCH
            a.matched_with = b
            a.save()
        changed += 1
    return changed


def guess_family_transfer(txn, names):
    if txn.category in ("", Category.EXPENSE, Category.INCOME) and any(n in txn.description.upper() for n in names):
        txn.category, txn.spend_category, txn.source = Category.TRANSFER, "", Transaction.Source.MATCH


def flow_amount(txn):
    """Bank debit of 50,000 to Zerodha = +50,000 into the Zerodha account."""
    return -txn.amount


@db_tx.atomic
def confirm(txn):
    """Confirm a transaction; investment rows become (or link to) a CashFlow on the investment account."""
    _unlink_flow(txn)
    if txn.category in (Category.INVESTMENT, Category.INVESTMENT_RETURN) and txn.investment_account_id:
        amount = flow_amount(txn)
        flow = CashFlow.objects.create(
            date=txn.date, account=txn.investment_account, amount=amount, source=CashFlow.Source.BANK,
            kind=CashFlow.Kind.INVEST if amount > 0 else CashFlow.Kind.WITHDRAW,
            from_account=txn.account if amount > 0 else None,
            note=f"Bank: {txn.description[:150]}", external_id=f"bank:{txn.pk}",
        )
        txn.cash_flow = flow
    txn.confirmed = True
    txn.save()
    if txn.cash_flow_id:
        reconcile(txn.investment_account)


def _unlink_flow(txn):
    if txn.cash_flow_id:
        flow = txn.cash_flow
        txn.cash_flow = None
        txn.save(update_fields=["cash_flow"])
        if flow.source == CashFlow.Source.BANK:
            flow.delete()


def reconcile(account):
    """Where a bank-statement flow duplicates Zerodha-ledger or CAS flows (same money seen twice),
    keep the more detailed ledger/CAS rows and drop the bank one. Returns number merged."""
    merged = 0
    claimed = set(CashFlow.objects.filter(account=account, bank_transactions__isnull=False)
                  .exclude(source=CashFlow.Source.BANK).values_list("id", flat=True))
    for bank_flow in CashFlow.objects.filter(account=account, source=CashFlow.Source.BANK).order_by("date"):
        window = (bank_flow.date - dt.timedelta(days=2), bank_flow.date + dt.timedelta(days=5))
        cands = [f for f in CashFlow.objects.filter(account=account, date__range=window)
                 .exclude(source__in=[CashFlow.Source.BANK, CashFlow.Source.MANUAL]).order_by("date")
                 if f.id not in claimed and (f.amount > 0) == (bank_flow.amount > 0)]
        tol = _tolerance(bank_flow.amount)
        match = next(([f] for f in cands if abs(f.amount - bank_flow.amount) <= tol), None)
        if match is None and cands and abs(sum(f.amount for f in cands) - bank_flow.amount) <= tol:
            match = cands
        if not match:
            continue
        Transaction.objects.filter(cash_flow=bank_flow).update(cash_flow=match[0])
        claimed.update(f.id for f in match)
        bank_flow.delete()
        merged += 1
    return merged
