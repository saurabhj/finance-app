from decimal import Decimal

from django.db import transaction as db_tx

from core.models import Account, AssetClass, Balance, Snapshot, Transaction

from . import schedule


def sync_account(loan):
    """Keep the Account's emi / rate / end date (used by the FI maths) in step with the schedule."""
    sch = schedule.for_loan(loan)
    cur = sch.current
    acc = loan.account
    acc.emi = cur.emi if cur and sch.months_left else None
    acc.interest_rate = cur.rate if cur else None
    acc.loan_end_date = sch.payoff_date
    acc.is_active = not loan.closed
    acc.save(update_fields=["emi", "interest_rate", "loan_end_date", "is_active"])
    return sch


@db_tx.atomic
def fill_snapshots(loan, overwrite=False):
    """Write the schedule's outstanding into month-end entries on/after the loan start.
    By default only fills entries that don't have this loan yet."""
    sch = schedule.for_loan(loan)
    n = 0
    for snap in Snapshot.objects.filter(date__gte=loan.start_date):
        value = sch.outstanding_on(snap.date).quantize(Decimal("0.01"))
        bal = Balance.objects.filter(snapshot=snap, account=loan.account).first()
        if bal and not overwrite:
            continue
        Balance.objects.update_or_create(snapshot=snap, account=loan.account, defaults={"value": value})
        n += 1
    return n


def create_account(name, owner):
    return Account.objects.create(owner=owner, name=name, kind=Account.Kind.LOAN,
                                  asset_class=AssetClass.LIABILITY, sort_order=90)


def matched_transactions(loan):
    """Bank-statement payments that mention this loan's keyword."""
    if not loan.statement_keyword.strip():
        return []
    rows = list(Transaction.objects.filter(description__icontains=loan.statement_keyword.strip(), amount__lt=0)
                .select_related("account").order_by("-date"))
    sch = schedule.for_loan(loan)
    recorded = set(loan.prepayments.exclude(transaction=None).values_list("transaction_id", flat=True))
    for t in rows:
        emi = next((r.emi for r in sch.rows if r.date >= t.date), sch.rows[-1].emi if sch.rows else None)
        t.looks_like_prepayment = bool(emi) and -t.amount > emi * Decimal("1.2")
        t.recorded_as_prepayment = t.id in recorded
    return rows
