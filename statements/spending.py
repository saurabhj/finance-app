"""Monthly spending from confirmed (and suggested) statement transactions."""
import datetime as dt
from collections import defaultdict
from decimal import Decimal

from core.models import Account, Category, MonthlyExpense, SpendCategory, StatementUpload, Transaction

ZERO = Decimal("0")


def _month(d):
    return d.replace(day=1)


def _month_end(m):
    return (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)


def days_covered(periods, start, end):
    """How many days from start to end fall inside at least one (from, to) period."""
    days = set()
    for a, b in periods:
        lo, hi = max(a, start), min(b, end)
        if lo <= hi:
            days.update(lo + dt.timedelta(days=i) for i in range((hi - lo).days + 1))
    return len(days)


def coverage(months):
    """For each month: list of bank/card accounts with statements that don't cover the whole month."""
    accounts = Account.objects.filter(statements__isnull=False).distinct()
    periods = defaultdict(list)
    for s in StatementUpload.objects.all():
        if s.period_start and s.period_end:
            periods[s.account_id].append((s.period_start, s.period_end))
    out = {}
    for m in months:
        start, end = m, _month_end(m)
        missing = []
        for acc in accounts:
            if days_covered(periods[acc.id], start, end) < (end - start).days + 1:
                missing.append(acc)
        out[m] = missing
    return out


def monthly(months_back=12, confirmed_only=False):
    qs = Transaction.objects.filter(category__in=[Category.EXPENSE, Category.REFUND, Category.LOAN_EMI])
    if confirmed_only:
        qs = qs.filter(confirmed=True)
    data = defaultdict(lambda: {"by_cat": defaultdict(lambda: ZERO), "spend": ZERO, "emi": ZERO, "unconfirmed": ZERO})
    for t in qs:
        m = _month(t.date)
        row = data[m]
        if t.category == Category.LOAN_EMI:
            row["emi"] += -t.amount
            continue
        amt = -t.amount  # debits are negative; refunds (credits) reduce spend
        row["spend"] += amt
        key = t.spend_category or ("refunds" if t.category == Category.REFUND else SpendCategory.OTHER)
        row["by_cat"][key] += amt
        if not t.confirmed:
            row["unconfirmed"] += amt
    months = sorted(data)[-months_back:]
    cov = coverage(months)
    labels = dict(SpendCategory.choices) | {"refunds": "Refunds & cashback"}
    rows = []
    for m in months:
        d = data[m]
        rows.append({
            "month": m, "spend": d["spend"], "emi": d["emi"], "unconfirmed": d["unconfirmed"],
            "by_cat": {labels.get(k, k): v for k, v in sorted(d["by_cat"].items(), key=lambda kv: -kv[1])},
            "missing": cov[m], "complete": not cov[m],
        })
    return rows


def suggestion(rows):
    """3-month average of complete months, and what to put on the Plan page."""
    complete = [r for r in rows if r["complete"]][-3:]
    if not complete:
        return None
    avg_spend = sum(r["spend"] for r in complete) / len(complete)
    avg_emi = sum(r["emi"] for r in complete) / len(complete)
    current = MonthlyExpense.objects.order_by("-month").first()
    include_emi = current.includes_emi if current else True
    if include_emi and not avg_emi:
        loan = Account.objects.filter(kind=Account.Kind.LOAN, is_active=True).first()
        avg_emi = loan.emi if loan and loan.emi else ZERO
    amount = avg_spend + (avg_emi if include_emi else ZERO)
    return {"months": [r["month"] for r in complete], "avg_spend": avg_spend, "avg_emi": avg_emi,
            "include_emi": include_emi, "amount": amount.quantize(Decimal("1")),
            "current": current.amount if current else None}


def adopt(amount, include_emi):
    MonthlyExpense.objects.update_or_create(
        month=dt.date.today().replace(day=1),
        defaults={"amount": amount, "includes_emi": include_emi, "note": "From statements (3-month average)"},
    )
