"""Saving the Plan form."""
import datetime as dt
from decimal import Decimal

from django.db import transaction

from core.models import Account, AllocationTarget, AssetClass, Balance, Household, MonthlyExpense, Snapshot

from .engine import DEFAULT_RETURNS
from .forms import CLASSES

HUNDRED = Decimal(100)


def initial_values():
    hh = Household.get()
    exp = MonthlyExpense.objects.order_by("-month").first()
    loan = Account.objects.filter(kind=Account.Kind.LOAN, is_active=True).first()
    latest = Snapshot.objects.order_by("-date").first()
    out = {
        "monthly_expenses": exp.amount if exp else None,
        "includes_emi": exp.includes_emi if exp else True,
        "expenses_from": exp.month if exp else dt.date.today().replace(day=1),
        "monthly_net_income": hh.monthly_net_income or None,
        "hours_worked_per_month": hh.hours_worked_per_month,
        "work_costs_monthly": hh.work_costs_monthly or None,
        "planned_monthly_investment": hh.planned_monthly_investment or None,
        "safe_withdrawal_rate": hh.safe_withdrawal_rate * HUNDRED,
        "inflation": hh.inflation * HUNDRED,
        "emergency_fund_months": hh.emergency_fund_months,
        "target_fi_date": hh.target_fi_date,
        "loan_backfill": True,
    }
    if loan:
        bal = Balance.objects.filter(account=loan, snapshot=latest).first() if latest else None
        out.update(loan_owner=loan.owner, loan_outstanding=bal.value if bal else None, loan_emi=loan.emi,
                   loan_rate=loan.interest_rate * HUNDRED if loan.interest_rate else None,
                   loan_end_date=loan.loan_end_date)
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

    if d.get("loan_outstanding"):
        save_loan(d["loan_owner"], d["loan_outstanding"], d.get("loan_emi"),
                  (d["loan_rate"] / HUNDRED) if d.get("loan_rate") else None, d.get("loan_end_date"),
                  backfill=d.get("loan_backfill"))


def save_loan(owner, outstanding, emi, rate, end_date, backfill=True):
    loan = Account.objects.filter(kind=Account.Kind.LOAN, is_active=True).first()
    if loan is None:
        loan = Account(name="Home loan", kind=Account.Kind.LOAN, asset_class=AssetClass.LIABILITY, sort_order=99)
    loan.owner, loan.emi, loan.interest_rate, loan.loan_end_date = owner, emi, rate, end_date
    loan.save()
    latest = Snapshot.objects.order_by("-date").first()
    if not latest:
        return loan
    Balance.objects.update_or_create(snapshot=latest, account=loan, defaults={"value": outstanding})
    if backfill and emi and rate:
        for snap in Snapshot.objects.filter(date__lt=latest.date):
            if Balance.objects.filter(snapshot=snap, account=loan).exists():
                continue
            Balance.objects.create(snapshot=snap, account=loan,
                                   value=estimate_past_outstanding(outstanding, emi, rate, snap.date, latest.date))
    return loan


def estimate_past_outstanding(outstanding, emi, rate, past, now):
    """Run the amortisation backwards: B(t-1) = (B(t) + EMI) / (1 + r/12)."""
    months = (now.year - past.year) * 12 + (now.month - past.month)
    b = Decimal(outstanding)
    r = Decimal(rate) / 12
    for _ in range(max(months, 0)):
        b = (b + Decimal(emi)) / (1 + r)
    return b.quantize(Decimal("0.01"))
