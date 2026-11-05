"""Saving the Plan form."""
import datetime as dt
from decimal import Decimal

from django.db import transaction

from core.models import Account, AllocationTarget, AssetClass, Balance, Household, MonthlyExpense, Snapshot

from .engine import DEFAULT_RETURNS
from .forms import CLASSES

HUNDRED = Decimal(100)


def _percent(rate):
    """0.0333 as 3.33, the two decimals the form accepts."""
    return (rate * HUNDRED).quantize(Decimal("0.01"))


def initial_values():
    hh = Household.get()
    exp = MonthlyExpense.objects.order_by("-month").first()
    latest = Snapshot.objects.order_by("-date").first()
    out = {
        "monthly_expenses": exp.amount if exp else None,
        "includes_emi": exp.includes_emi if exp else True,
        "expenses_from": exp.month if exp else dt.date.today().replace(day=1),
        "monthly_net_income": hh.monthly_net_income or None,
        "hours_worked_per_month": hh.hours_worked_per_month,
        "work_costs_monthly": hh.work_costs_monthly or None,
        "planned_monthly_investment": hh.planned_monthly_investment or None,
        "safe_withdrawal_rate": _percent(hh.safe_withdrawal_rate),
        "inflation": _percent(hh.inflation),
        "emergency_fund_months": hh.emergency_fund_months,
        "target_fi_date": hh.target_fi_date,
    }
    targets = {t.asset_class: t for t in AllocationTarget.objects.all()}
    for c in CLASSES:
        t = targets.get(c)
        out[f"target_{c}"] = t.target_pct if t else None
        out[f"band_{c}"] = t.band_pct if t else Decimal(5)
        r = (hh.expected_returns or {}).get(c, DEFAULT_RETURNS.get(c))
        out[f"return_{c}"] = Decimal(str(round(r * 100, 2))) if r is not None else None
    return out


@transaction.atomic
def save_plan(d):
    hh = Household.get()
    hh.monthly_net_income = d["monthly_net_income"] or 0
    hh.hours_worked_per_month = d["hours_worked_per_month"]
    hh.work_costs_monthly = d["work_costs_monthly"] or 0
    hh.planned_monthly_investment = d["planned_monthly_investment"] or 0
    hh.safe_withdrawal_rate = d["safe_withdrawal_rate"] / HUNDRED
    hh.inflation = d["inflation"] / HUNDRED
    hh.emergency_fund_months = d["emergency_fund_months"]
    hh.target_fi_date = d["target_fi_date"]
    hh.expected_returns = {c: float(d[f"return_{c}"]) / 100 for c in CLASSES if d.get(f"return_{c}") is not None}
    hh.save()

    MonthlyExpense.objects.update_or_create(
        month=d["expenses_from"].replace(day=1),
        defaults={"amount": d["monthly_expenses"], "includes_emi": d["includes_emi"]},
    )

    for c in CLASSES:
        if d.get(f"target_{c}") is None:
            AllocationTarget.objects.filter(asset_class=c).delete()
        else:
            AllocationTarget.objects.update_or_create(
                asset_class=c, defaults={"target_pct": d[f"target_{c}"], "band_pct": d.get(f"band_{c}") or 5})

