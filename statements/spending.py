"""Monthly spending from confirmed (and suggested) statement transactions."""
import datetime as dt
from collections import defaultdict
from decimal import Decimal

from django.db.models import Max, Min, Sum

from core.models import Account, Category, MonthlyExpense, SpendCategory, StatementUpload, Transaction

ZERO = Decimal("0")
GRACE_DAYS = 3  # a cycle ending a day or two before month-end still counts as covering the month
SPEND_ROWS = [Category.EXPENSE, Category.REFUND, Category.LOAN_EMI, Category.HOME]
LABELS = dict(SpendCategory.choices) | {"refunds": "Refunds & cashback", "emi": "Loan EMI", "home": Category.HOME.label}


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


def statement_periods():
    """Each account's statement periods; uploads without printed dates fall back to their transactions."""
    periods = defaultdict(list)
    for s in StatementUpload.objects.annotate(first=Min("transactions__date"), last=Max("transactions__date")):
        lo, hi = s.period_start or s.first, s.period_end or s.last
        if lo and hi:
            periods[s.account_id].append((lo, hi))
    return periods


def days_needed(account, periods, month):
    """(days of this month the account should have statements for, days it does have)."""
    start, end = month, _month_end(month)
    since = account.statements_from
    if since and end < since:
        return 0, 0
    if since and _month(since) == month and periods:  # first month: only from the first statement on
        first = min(p[0] for p in periods)
        if start <= first <= end:
            start = first
    return (end - start).days + 1, days_covered(periods, start, end)


def coverage(months):
    """For each month: active bank/card accounts with statements that don't cover it, allowing a few days' slack."""
    accounts = Account.objects.filter(statements__isnull=False, is_active=True).distinct()
    periods = statement_periods()
    out = {}
    for m in months:
        out[m] = []
        for acc in accounts:
            needed, got = days_needed(acc, periods[acc.id], m)
            if needed - got > GRACE_DAYS:
                out[m].append(acc)
    return out


def _spend_key(t):
    return t.spend_category or ("refunds" if t.category == Category.REFUND else SpendCategory.OTHER)


def monthly(months_back=12, confirmed_only=False):
    qs = Transaction.objects.filter(category__in=SPEND_ROWS)
    if confirmed_only:
        qs = qs.filter(confirmed=True)
    data = defaultdict(lambda: {"by_cat": defaultdict(lambda: ZERO), "spend": ZERO, "emi": ZERO, "home": ZERO,
                                "unconfirmed": ZERO})
    for t in qs:
        m = _month(t.date)
        row = data[m]
        if t.category == Category.LOAN_EMI:
            row["emi"] += -t.amount
            continue
        if t.category == Category.HOME:  # one-off, so kept out of the monthly figure
            row["home"] += -t.amount
            continue
        amt = -t.amount  # debits are negative; refunds (credits) reduce spend
        row["spend"] += amt
        row["by_cat"][_spend_key(t)] += amt
        if not t.confirmed:
            row["unconfirmed"] += amt
    months = sorted(data)[-months_back:]
    cov = coverage(months)
    periods = statement_periods()
    rows = []
    for m in months:
        d = data[m]
        rows.append({
            "month": m, "spend": d["spend"], "emi": d["emi"], "home": d["home"], "unconfirmed": d["unconfirmed"],
            "by_cat": {LABELS.get(k, k): v for k, v in sorted(d["by_cat"].items(), key=lambda kv: -kv[1])},
            "missing": cov[m],
            "gaps": [{"account": a, "before_first": _month_end(m) < min(p[0] for p in periods[a.id])} for a in cov[m]],
            "complete": not cov[m] and m < _month(dt.date.today()),
            "in_progress": m >= _month(dt.date.today()), "next_month": _month_end(m) + dt.timedelta(days=1),
        })
    return rows


def month_detail(month, unconfirmed_only=False, by_date=False):
    """The rows behind one month on the Spending page, grouped by spend type, biggest first.
    Totals always cover the whole month; `unconfirmed_only` only hides rows."""
    qs = (Transaction.objects.filter(category__in=SPEND_ROWS, date__range=(month, _month_end(month)))
          .select_related("account__owner", "investment_account", "matched_with__account"))
    groups = {}
    for t in qs:
        key = {Category.LOAN_EMI: "emi", Category.HOME: "home"}.get(t.category) or _spend_key(t)
        g = groups.setdefault(key, {"key": key, "label": LABELS.get(key, key), "total": ZERO, "count": 0,
                                    "unconfirmed": 0, "rows": []})
        g["total"] += -t.amount
        g["count"] += 1
        g["unconfirmed"] += not t.confirmed
        if not (unconfirmed_only and t.confirmed):
            g["rows"].append(t)
    for g in groups.values():
        g["rows"].sort(key=(lambda t: (t.date, t.id)) if by_date else (lambda t: -abs(t.amount)))
    emi, home = groups.pop("emi", None), groups.pop("home", None)
    ordered = sorted(groups.values(), key=lambda g: -g["total"]) + [g for g in (emi, home) if g]
    return {"groups": ordered, "spend": sum((g["total"] for g in groups.values()), ZERO),
            "emi": emi["total"] if emi else ZERO, "home": home["total"] if home else ZERO, "unconfirmed": sum(g["unconfirmed"] for g in ordered),
            "count": sum(g["count"] for g in ordered), "missing": coverage([month])[month]}


def home_total():
    return -(Transaction.objects.filter(category=Category.HOME).aggregate(s=Sum("amount"))["s"] or ZERO)


def month_neighbours(month):
    """The months before and after this one, if there's any spending that far out."""
    span = Transaction.objects.filter(category__in=SPEND_ROWS).aggregate(first=Min("date"), last=Max("date"))
    prev_month = _month(month - dt.timedelta(days=1))
    next_month = _month_end(month) + dt.timedelta(days=1)
    return (prev_month if span["first"] and span["first"] < month else None,
            next_month if span["last"] and span["last"] >= next_month else None)


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
