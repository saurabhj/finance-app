import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction as db_tx
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import Account, Loan, LoanCheckpoint, LoanPrepayment, LoanRate, Transaction

from core.templatetags.money import inr

from . import schedule, services
from .forms import CheckpointForm, LoanForm, PrepaymentForm, RateForm

ZERO = Decimal("0")


def _summary(loan, today):
    sch = schedule.for_loan(loan, today)
    return {"loan": loan, "sch": sch, "fy": sch.fy_totals(today),
            "savings": schedule.savings_from_prepayments(loan, today)}


@login_required
def loan_list(request):
    today = timezone.localdate()
    loans = [_summary(l, today) for l in Loan.objects.select_related("account__owner").filter(closed=False)]
    closed = Loan.objects.select_related("account__owner").filter(closed=True)
    missing = Account.objects.filter(kind=Account.Kind.LOAN, loan__isnull=True).select_related("owner")
    totals = {
        "outstanding": sum((x["sch"].outstanding for x in loans), ZERO),
        "emi": sum(((x["sch"].current.emi if x["sch"].months_left else ZERO) for x in loans), ZERO),
        "interest_fy": sum(((x["fy"] or {}).get("interest", ZERO) for x in loans), ZERO),
        "interest_left": sum((x["sch"].interest_remaining for x in loans), ZERO),
        "debt_free": max((x["sch"].payoff_date for x in loans if x["sch"].payoff_date), default=None),
    }
    return render(request, "core/loans.html", {"loans": loans, "closed": closed, "missing": missing,
                                               "totals": totals, "today": today})


@login_required
def loan_detail(request, pk):
    loan = get_object_or_404(Loan.objects.select_related("account__owner"), pk=pk)
    today = timezone.localdate()
    s = _summary(loan, today)
    sch = s["sch"]
    without = schedule.for_loan(loan, today, include_prepayments=False) if s["savings"] else None
    by_year = sch.by_fy()
    chart = {
        "labels": [r.date.strftime("%b %Y") for r in sch.rows],
        "balance": [float(r.closing) for r in sch.rows],
        "without": ([float(without.outstanding_on(r.date)) for r in sch.rows] if without else None),
        "today_index": len(sch.past) - 1,
        "fy_labels": [t["fy"] for t in by_year],
        "fy_interest": [float(t["interest"]) for t in by_year],
        "fy_principal": [float(t["principal"] + t["prepayment"]) for t in by_year],
    }
    return render(request, "core/loan_detail.html", {
        **s, "by_year": by_year, "chart_json": json.dumps(chart), "today": today,
        "rate_form": RateForm(initial={"effective_date": today}),
        "prepay_form": PrepaymentForm(initial={"date": today}),
        "checkpoint_form": CheckpointForm(initial={"date": today}),
        "transactions": services.matched_transactions(loan),
        "show_all": request.GET.get("rows") == "all",
        "lo": max(1, len(sch.past) - 5), "hi": len(sch.past) + 13, "next_n": len(sch.past) + 1,
        "borrowed_sub": f"of {inr(loan.principal)} borrowed",
        "fy_sub": f"principal {inr(s['fy']['principal'] + s['fy']['prepayment'])}" if s["fy"] else "",
    })


@login_required
def loan_edit(request, pk=None):
    loan = get_object_or_404(Loan, pk=pk) if pk else None
    account = loan.account if loan else None
    if not loan and request.GET.get("account"):  # add details to an existing loan account (e.g. from the Plan page)
        account = get_object_or_404(Account, pk=request.GET["account"], kind=Account.Kind.LOAN, loan__isnull=True)
    form = LoanForm(request.POST or None, instance=loan, account=account)
    if request.method == "POST" and form.is_valid():
        with db_tx.atomic():
            d = form.cleaned_data
            acc = account or services.create_account(d["name"], d["owner"])
            acc.name, acc.owner = d["name"], d["owner"]
            acc.save(update_fields=["name", "owner"])
            obj = form.save(commit=False)
            obj.account = acc
            obj.save()
            if not loan:
                LoanRate.objects.create(loan=obj, effective_date=obj.start_date, rate=d["initial_rate"], note="At start")
            services.sync_account(obj)
            filled = services.fill_snapshots(obj)
        messages.success(request, f"Saved {acc.name}." + (f" Outstanding filled into {filled} month-end entr{'y' if filled == 1 else 'ies'}." if filled else ""))
        return redirect("loan_detail", pk=obj.pk)
    return render(request, "core/loan_form.html", {"form": form, "loan": loan})


def _child(request, pk, form_cls, label):
    loan = get_object_or_404(Loan, pk=pk)
    form = form_cls(request.POST)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.loan = loan
        obj.save()
        services.sync_account(loan)
        messages.success(request, f"{label} added.")
    else:
        messages.error(request, f"{label} not added: " + "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    return redirect("loan_detail", pk=loan.pk)


@login_required
@require_POST
def add_rate(request, pk):
    return _child(request, pk, RateForm, "Rate change")


@login_required
@require_POST
def add_prepayment(request, pk):
    return _child(request, pk, PrepaymentForm, "Prepayment")


@login_required
@require_POST
def add_checkpoint(request, pk):
    return _child(request, pk, CheckpointForm, "Bank balance")


@login_required
@require_POST
def delete_item(request, pk, kind, item):
    model = {"rate": LoanRate, "prepayment": LoanPrepayment, "checkpoint": LoanCheckpoint}[kind]
    loan = get_object_or_404(Loan, pk=pk)
    if kind == "rate" and loan.rates.count() <= 1:
        messages.error(request, "A loan needs at least one rate.")
    else:
        get_object_or_404(model, pk=item, loan=loan).delete()
        services.sync_account(loan)
        messages.info(request, "Deleted.")
    return redirect("loan_detail", pk=loan.pk)


@login_required
@require_POST
def prepayment_from_transaction(request, pk, txn):
    loan = get_object_or_404(Loan, pk=pk)
    t = get_object_or_404(Transaction, pk=txn)
    LoanPrepayment.objects.get_or_create(loan=loan, transaction=t, defaults={
        "date": t.date, "amount": -t.amount, "note": t.description[:200]})
    services.sync_account(loan)
    messages.success(request, f"Recorded ₹{-t.amount:,.0f} on {t.date:%d %b %Y} as a prepayment. Edit or delete it below if only part of it was.")
    return redirect("loan_detail", pk=loan.pk)


@login_required
@require_POST
def refresh_snapshots(request, pk):
    loan = get_object_or_404(Loan, pk=pk)
    n = services.fill_snapshots(loan, overwrite=True)
    messages.success(request, f"Updated the outstanding in {n} month-end entr{'y' if n == 1 else 'ies'} from the schedule.")
    return redirect("loan_detail", pk=loan.pk)
