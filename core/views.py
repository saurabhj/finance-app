import datetime as dt
import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render

from . import analytics
from .forms import CashFlowForm, SnapshotForm
from .models import Account, Balance, CashFlow, HoldingsPull, Snapshot


def _f(v):
    return float(v) if v is not None else None


@login_required
def dashboard(request):
    periods = analytics.periods()
    latest = periods[-1] if periods else None
    tracked = [p for p in periods if p.market_gain is not None]
    chart = {
        "labels": [p.snapshot.date.strftime("%b %Y") for p in periods],
        "net_worth": [_f(p.net_worth) for p in periods],
        "persons": {
            name: [_f(p.by_person.get(name)) for p in periods]
            for name in (latest.by_person if latest else {})
        },
        "invested": [_f(p.investments.invested) if p.market_gain is not None else None for p in periods],
        "gain": [_f(p.market_gain) for p in periods],
        "untracked_change": [
            _f(p.change) if p.prev and p.market_gain is None else None for p in periods
        ],
    }
    alloc = analytics.allocation(latest.snapshot) if latest else []
    chart["alloc_labels"] = [a["label"] for a in alloc]
    chart["alloc_values"] = [_f(a["value"]) for a in alloc]
    from .models import Recommendation, Review
    ctx = {
        "latest_review": Review.objects.filter(status="done").exclude(cadence="adhoc").first(),
        "open_actions": Recommendation.objects.filter(status="open").exclude(verdict="hold").count(),
        "latest": latest,
        "periods": list(reversed(periods)),
        "allocation": alloc,
        "chart_json": json.dumps(chart),
        "ytd": {
            "invested": sum((p.investments.invested for p in tracked), Decimal(0)),
            "gain": sum((p.market_gain for p in tracked), Decimal(0)),
            "months": len(tracked),
        },
    }
    return render(request, "core/dashboard.html", ctx)


@login_required
def snapshot_entry(request, date=None):
    accounts = Account.objects.filter(is_active=True).exclude(kind=Account.Kind.CARD).select_related("owner")
    snap = get_object_or_404(Snapshot, date=date) if date else None
    if snap:
        values = dict(snap.balances.values_list("account_id", "value"))
        prev = Snapshot.objects.filter(date__lt=snap.date).order_by("-date").first()
        flows = _flows_between(prev.date if prev else None, snap.date)
        initial = {"date": snap.date, "note": snap.note}
    else:
        last = Snapshot.objects.order_by("-date").first()
        values = dict(last.balances.values_list("account_id", "value")) if last else {}
        today = dt.date.today()
        values.update(_values_from_pulls(accounts, today))
        flows = _flows_between(last.date if last else None, today)
        initial = {"date": today}

    form = SnapshotForm(
        request.POST or None, accounts=accounts, initial=initial,
        initial_values=values, initial_flows=flows,
    )
    if request.method == "POST" and form.is_valid():
        saved = _save_snapshot(form, existing=snap)
        messages.success(request, f"Saved values for {saved.date:%d %b %Y}.")
        return redirect("dashboard")

    prev_label = None
    if not snap:
        last = Snapshot.objects.order_by("-date").first()
        prev_label = last.date if last else None
    return render(request, "core/snapshot_form.html", {
        "form": form, "snapshot": snap, "prefilled_from": prev_label,
    })


def _latest_pull(account, date, max_age_days):
    return HoldingsPull.objects.filter(
        account=account, as_of__lte=date, as_of__gte=date - dt.timedelta(days=max_age_days),
    ).first()


def _values_from_pulls(accounts, date):
    """Fresh Kite pulls (≤3 days) and CAS holdings revalued at today's NAV pre-fill the form."""
    out = {}
    for acc in accounts:
        if acc.kind == Account.Kind.BROKER:
            pull = _latest_pull(acc, date, 3)
            if pull:
                out[acc.id] = pull.total_value
        elif acc.kind == Account.Kind.MF:
            pull = _latest_pull(acc, date, 60)
            if pull:
                out[acc.id] = pull.current_value()
        elif acc.kind in (Account.Kind.FD, Account.Kind.OTHER):
            # deposits don't move with markets: last value + money put in/taken out since
            last = Balance.objects.filter(account=acc, snapshot__date__lt=date).order_by("-snapshot__date").first()
            if last:
                flows = CashFlow.objects.filter(account=acc, date__gt=last.snapshot.date, date__lte=date)
                out[acc.id] = last.value + (flows.aggregate(t=Sum("amount"))["t"] or 0)
        elif acc.kind == Account.Kind.LOAN and hasattr(acc, "loan"):
            from loans.schedule import for_loan
            out[acc.id] = for_loan(acc.loan, today=date).outstanding_on(date).quantize(Decimal("0.01"))
        elif acc.kind == Account.Kind.BANK:
            txn = (acc.transactions.filter(balance__isnull=False, date__lte=date,
                                           date__gte=date - dt.timedelta(days=10)).order_by("-date", "-id").first())
            if txn:
                out[acc.id] = txn.balance
    return out


def _flows_between(start, end):
    qs = CashFlow.objects.filter(date__lte=end)
    if start:
        qs = qs.filter(date__gt=start)
    return dict(qs.values("account_id").annotate(t=Sum("amount")).values_list("account_id", "t"))


@transaction.atomic
def _save_snapshot(form, existing=None):
    date = form.cleaned_data["date"]
    snap = existing or Snapshot.objects.filter(date=date).first()
    if snap is None:
        snap = Snapshot(date=date, source=Snapshot.Source.MANUAL)
    prev = Snapshot.objects.filter(date__lt=date).exclude(pk=snap.pk).order_by("-date").first()
    old_date = snap.date
    snap.date = date
    snap.note = form.cleaned_data["note"]
    snap.flows_tracked = True
    snap.save()

    snap.balances.all().delete()
    accounts = {a.id: a for a in form.accounts}
    Balance.objects.bulk_create(
        Balance(snapshot=snap, account_id=acc_id, value=v,
                pull=_latest_pull(accounts[acc_id], date, 45) if accounts[acc_id].is_investment else None)
        for acc_id, v in form.values().items()
    )
    # Flows entered on this form are stored as one row per account dated on the
    # snapshot date. Replace any previous form-entered rows for this period.
    start = prev.date if prev else None
    period = CashFlow.objects.filter(
        source=CashFlow.Source.MANUAL, note="Monthly entry",
        date__lte=max(date, old_date),
    )
    if start:
        period = period.filter(date__gt=start)
    period.delete()
    # Other manual flows in the period (entered on the Money moved page) still count;
    # the form shows their total, so only store the difference.
    others = _flows_between(start, date)
    for acc_id, total in form.flows().items():
        delta = total - others.get(acc_id, Decimal(0))
        if delta:
            CashFlow.objects.create(
                date=date, account_id=acc_id, amount=delta,
                kind=CashFlow.Kind.INVEST if delta > 0 else CashFlow.Kind.WITHDRAW,
                note="Monthly entry",
            )
    return snap


@login_required
def cashflows(request):
    form = CashFlowForm(request.POST or None, initial={"date": dt.date.today()})
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Recorded.")
        return redirect("cashflows")
    flows = CashFlow.objects.select_related("account__owner", "from_account")[:200]
    return render(request, "core/cashflows.html", {"form": form, "flows": flows})


@login_required
def cashflow_delete(request, pk):
    if request.method == "POST":
        get_object_or_404(CashFlow, pk=pk).delete()
        messages.info(request, "Deleted.")
    return redirect("cashflows")


# ---------------------------------------------------------------- holdings & integrations
from django.views.decorators.http import require_POST  # noqa: E402

from integrations import amfi, cas, kite, zerodha_ledger  # noqa: E402

from .models import Instrument, Person  # noqa: E402


@login_required
def holdings(request):
    accounts = Account.investment_accounts().filter(is_active=True).select_related("owner")
    blocks = []
    for acc in accounts:
        pull = acc.pulls.first()
        if not pull:
            continue
        positions = list(pull.positions.select_related("instrument").order_by("-value"))
        blocks.append({"account": acc, "pull": pull, "positions": positions,
                       "current_value": pull.current_value(), "xirr": analytics.xirr_for([acc])})
    ctx = {
        "blocks": blocks,
        "kite_status": kite.people_status(),
        "people": Person.objects.people(),
        "zerodha_accounts": accounts.filter(kind=Account.Kind.BROKER),
        "mf_accounts": accounts.filter(kind=Account.Kind.MF),
        "needs_review": Instrument.objects.filter(needs_review=True, positions__isnull=False).distinct().count(),
        "management_choices": Instrument.Management.choices,
        "nav_date": Instrument.objects.filter(kind="mf").exclude(last_price_date=None)
                    .order_by("-last_price_date").values_list("last_price_date", flat=True).first(),
        "family_xirr": analytics.xirr_for(Account.investment_accounts()),
    }
    return render(request, "core/holdings.html", ctx)


@login_required
@require_POST
def instruments_update(request):
    """Bulk-save management tags from the Holdings page and mark them reviewed."""
    for inst in Instrument.objects.filter(id__in=request.POST.getlist("instrument")):
        mgmt = request.POST.get(f"mgmt_{inst.id}")
        if mgmt in Instrument.Management.values:
            inst.management = mgmt
        inst.advisor = request.POST.get(f"advisor_{inst.id}", inst.advisor)[:80]
        inst.needs_review = False
        inst.save(update_fields=["management", "advisor", "needs_review"])
    messages.success(request, "Tags saved.")
    return redirect("holdings")


@login_required
def zerodha_connect(request, person_id):
    person = get_object_or_404(Person, pk=person_id)
    try:
        url = kite.login_url(person)
    except kite.KiteNotConfigured as e:
        messages.error(request, str(e))
        return redirect("holdings")
    request.session["kite_person"] = person.id
    return redirect(url)


@login_required
def zerodha_callback(request):
    person_id = request.GET.get("person") or request.session.get("kite_person")
    person = get_object_or_404(Person, pk=person_id)
    if request.GET.get("status") != "success" or not request.GET.get("request_token"):
        messages.error(request, "Zerodha login was cancelled.")
        return redirect("holdings")
    try:
        kite.complete_login(person, request.GET["request_token"])
        pull = kite.pull_holdings(person)
    except Exception as e:  # surface Kite errors on the page
        messages.error(request, f"Zerodha: {e}")
        return redirect("holdings")
    messages.success(request, f"Pulled {pull.positions.count()} holdings for {person.name}: ₹{pull.total_value:,.0f}.")
    return redirect("holdings")


@login_required
@require_POST
def zerodha_pull(request, person_id):
    person = get_object_or_404(Person, pk=person_id)
    try:
        pull = kite.pull_holdings(person)
    except kite.KiteLoginRequired:
        return redirect("zerodha_connect", person_id=person.id)
    except Exception as e:
        messages.error(request, f"Zerodha: {e}")
        return redirect("holdings")
    messages.success(request, f"Pulled {pull.positions.count()} holdings for {person.name}: ₹{pull.total_value:,.0f}.")
    return redirect("holdings")


@login_required
@require_POST
def ledger_upload(request):
    account = get_object_or_404(Account, pk=request.POST.get("account"), kind=Account.Kind.BROKER)
    f = request.FILES.get("file")
    if not f:
        messages.error(request, "Choose the ledger CSV file.")
        return redirect("holdings")
    try:
        created, skipped = zerodha_ledger.import_ledger(account, f.read().decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        messages.error(request, f"Couldn't read that ledger: {e}")
        return redirect("holdings")
    messages.success(request, f"{account}: {created} pay-ins/payouts added, {skipped} already known.")
    return redirect("holdings")


@login_required
@require_POST
def cas_upload(request):
    account = get_object_or_404(Account, pk=request.POST.get("account"), kind=Account.Kind.MF)
    f = request.FILES.get("file")
    if not f:
        messages.error(request, "Choose the CAS PDF.")
        return redirect("holdings")
    try:
        data = cas.read_pdf(f, request.POST.get("password", ""))
        pull, created, skipped = cas.import_cas(data, account)
    except Exception as e:  # casparser raises its own error types for bad passwords/formats
        messages.error(request, f"Couldn't read that CAS: {e}")
        return redirect("holdings")
    total = f"₹{pull.total_value:,.0f} in {pull.positions.count()} schemes" if pull else "no current holdings"
    messages.success(request, f"{account}: {total}; {created} transactions added, {skipped} already known.")
    return redirect("holdings")


@login_required
@require_POST
def nav_refresh(request):
    try:
        n = amfi.update_navs()
        messages.success(request, f"Updated NAVs for {n} funds.")
    except Exception as e:
        messages.error(request, f"AMFI NAV fetch failed: {e}")
    return redirect("holdings")
