"""Builds FI inputs and chart data from the database."""
from decimal import Decimal

from core import analytics
from core.models import Account, AllocationTarget, AssetClass, Household, MonthlyExpense, Snapshot

from .engine import FiInputs

MIN_TRACKED_PERIODS = 3


def expense_on(date):
    """Monthly expense in force on a date (latest entry on/before it, else the earliest one)."""
    e = MonthlyExpense.objects.filter(month__lte=date).order_by("-month").first()
    return e or MonthlyExpense.objects.order_by("month").first()


def loan_accounts():
    return Account.objects.filter(kind=Account.Kind.LOAN, is_active=True)


def actual_monthly_investment(periods):
    """Average money invested per month over tracked periods (None if too few)."""
    tracked = [p for p in periods if p.market_gain is not None]
    if len(tracked) < MIN_TRACKED_PERIODS:
        return None
    days = sum((p.snapshot.date - p.prev.date).days for p in tracked)
    invested = sum(float(p.investments.invested) for p in tracked)
    return invested / (days / 30.44) if days else None


def build_inputs(snapshot=None, periods=None):
    """None if there is no snapshot or no expense figure yet."""
    snapshot = snapshot or Snapshot.objects.order_by("-date").first()
    expense = expense_on(snapshot.date) if snapshot else None
    if not snapshot or not expense:
        return None
    hh = Household.get()
    periods = periods if periods is not None else analytics.periods()
    balances = {b.account_id: float(b.value) for b in snapshot.balances.all()}
    loans = list(loan_accounts())
    loan_out = sum(balances.get(l.id, 0.0) for l in loans)
    emi = sum(float(l.emi or 0) for l in loans)
    rate = next((float(l.interest_rate) for l in loans if l.interest_rate), 0.0)
    bank = sum(float(b.value) for b in snapshot.balances.select_related("account") if b.account.kind == Account.Kind.BANK)
    invest = {a["asset_class"]: float(a["value"]) for a in analytics.allocation(snapshot, investments_only=True)}
    actual = actual_monthly_investment(periods)
    return FiInputs(
        as_of=snapshot.date, investments=invest, bank_cash=bank,
        monthly_expenses=float(expense.amount), expenses_include_emi=expense.includes_emi,
        loan_outstanding=loan_out, emi=emi, loan_rate=rate,
        monthly_investment=actual if actual is not None else float(hh.planned_monthly_investment),
        swr=float(hh.safe_withdrawal_rate), inflation=float(hh.inflation),
        emergency_months=hh.emergency_fund_months, returns=hh.expected_returns or {},
    ), ("actual" if actual is not None else "planned")


def wall_chart_history(periods):
    """Per snapshot: safe monthly income vs core expenses (YMOYL wall chart)."""
    rows = []
    for p in periods:
        built = build_inputs(p.snapshot, periods)
        if not built:
            continue
        inp, _ = built
        rows.append({"date": p.snapshot.date, "safe_income": inp.safe_monthly_income,
                     "expenses": inp.core_expenses, "progress": inp.progress})
    return rows


def drift(inp):
    """Actual vs target allocation across all assets (investments + all bank cash)."""
    mix = dict(inp.investments)
    mix["cash"] = mix.get("cash", 0.0) + inp.bank_cash
    total = sum(mix.values()) or 1.0
    targets = {t.asset_class: t for t in AllocationTarget.objects.all()}
    labels = dict(AssetClass.choices)
    out = []
    for k in sorted(set(mix) | set(targets), key=lambda c: -mix.get(c, 0)):
        actual = mix.get(k, 0.0) / total
        t = targets.get(k)
        target = float(t.target_pct) / 100 if t else None
        band = float(t.band_pct) / 100 if t else None
        out.append({
            "asset_class": k, "label": labels.get(k, k), "value": mix.get(k, 0.0), "actual": actual,
            "target": target, "diff": (actual - target) if target is not None else None,
            "out_of_band": target is not None and abs(actual - target) > band,
            "rebalance_amount": (target - actual) * total if target is not None else None,
        })
    return out


def savings_rates(periods, monthly_income):
    """YMOYL: share of income saved, per tracked period."""
    out = []
    for p in periods:
        if p.savings is None or not monthly_income:
            continue
        months = (p.snapshot.date - p.prev.date).days / 30.44
        out.append({"date": p.snapshot.date, "savings": float(p.savings),
                    "rate": float(p.savings) / (monthly_income * months) if months else None})
    return out
