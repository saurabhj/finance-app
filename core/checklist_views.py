"""Checklist page: what to upload for the app to work well, and an on-demand check of what's missing.

The check only reads the database (no AI, no network), and runs when the button is pressed.
"""
import datetime as dt
from dataclasses import dataclass, field

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Min
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from integrations import kite
from statements.spending import _month, _month_end, days_covered

from .models import (Account, AllocationTarget, Balance, CashFlow, Category, Household, HoldingsPull, Loan,
                     LoanCheckpoint, MonthlyExpense, Person, Snapshot, StatementUpload, Transaction)

START = dt.date(2025, 6, 1)  # statements are wanted from here on, unless an account says otherwise
GRACE_DAYS = 3               # card cycles rarely line up with the calendar: a few uncovered days still count
STALE_DAYS = 45

OK, PARTIAL, MISSING = "ok", "partial", "missing"
STATUS_LABELS = {OK: "Done", PARTIAL: "Partly", MISSING: "Missing"}

ITEMS = {
    "core": [
        {"key": "bank", "title": "Bank statements, every account, every month",
         "why": "Shows your real income, what you spend from the bank, and what went to investments and loans. "
                "Also fills in bank balances on the month-end form.",
         "how": "Netbanking → statements. CSV, Excel or PDF all work.", "link": "Upload", "url": "statements"},
        {"key": "card", "title": "Credit card statements, every billing cycle",
         "why": "Most day-to-day spending sits on cards. Without them Spending runs low, and your FI target "
                "is built on an expense number that's too small.",
         "how": "Card netbanking or the emailed PDF.", "link": "Upload", "url": "statements"},
        {"key": "ledger", "title": "Zerodha ledger, for each of you",
         "why": "Separates money you added from market gain, which is what makes XIRR and the "
                "invested-vs-gain view correct for Zerodha. Also matches the bank transfers so nothing counts twice.",
         "how": "console.zerodha.com → Funds → Statement → Ledger → CSV.", "link": "Upload", "url": "holdings"},
        {"key": "kite", "title": "Zerodha holdings, for each of you",
         "why": "Splits Zerodha into equity, gold and funds. Asset allocation, drift and the exit / hold / add "
                "calls on self-managed holdings all depend on this.",
         "how": "Connect on the Holdings page. Zerodha needs a fresh login each day.", "link": "Connect", "url": "holdings"},
        {"key": "cas", "title": "Mutual fund CAS, for each of you",
         "why": "Gives fund-level holdings and every purchase and redemption, covering ET Money and the older "
                "active funds. Needed to compare funds with the index and to track money invested.",
         "how": "mfcentral.com → detailed CAS PDF, then upload with its password.", "link": "Upload", "url": "holdings"},
        {"key": "monthend", "title": "Month-end balances",
         "why": "This is your net worth history. It's also the only source of values for FDs, the ULIP and "
                "anything else that has no statement here.",
         "how": "Not a document: fill the form on the 1st. Most of it prefills.", "link": "Open", "url": "snapshot_new"},
        {"key": "loans", "title": "Loan details",
         "why": "Outstanding balance, EMI and rate history feed the amortisation schedule, your net worth and "
                "the FI target, and show when each EMI frees up.",
         "how": "Sanction letter or latest loan statement.", "link": "Open", "url": "loans"},
    ],
    "extra": [
        {"key": "checkpoint", "title": "Loan statements or interest certificates",
         "why": "Entered as a checkpoint, the lender's actual outstanding corrects small drift between the "
                "calculated schedule and the real balance.",
         "how": "Lender's portal, or the yearly interest certificate.", "link": "Open", "url": "loans"},
        {"key": "values", "title": "FD and ULIP statements",
         "why": "Keeps these at their real values. Between entries the app can only carry the last value forward, "
                "so returns on them look flat.",
         "how": "Enter the values on the month-end form.", "link": "Open", "url": "snapshot_new"},
        {"key": "reimburse", "title": "Statements for any card used for reimbursed purchases",
         "why": "Reimbursements pair up with the original purchase, so they stay out of Spending and you can see "
                "what's still owed. A credit with no purchase usually means that card's statement is missing.",
         "how": "Add the card on Accounts first, then upload its statements.", "link": "Upload", "url": "statements"},
        {"key": "plan", "title": "Plan numbers",
         "why": "Net income drives the savings rate and real hourly wage. Target allocation is what reviews "
                "measure drift against, so without it they can't suggest rebalancing.",
         "how": "A recent payslip for net income; targets are your call.", "link": "Open", "url": "plan"},
    ],
}


@dataclass
class Result:
    status: str
    detail: str = ""
    todos: list = field(default_factory=list)  # (text, url name)


def month_label(m):
    return m.strftime("%b %Y")


def month_ranges(months):
    """[Jun, Jul, Aug, Oct] -> ["Jun 2025 – Aug 2025", "Oct 2025"]."""
    groups = []
    for m in months:
        if groups and _month(groups[-1][-1] + dt.timedelta(days=32)) == m:
            groups[-1].append(m)
        else:
            groups.append([m])
    return [month_label(g[0]) if len(g) == 1 else f"{month_label(g[0])} – {month_label(g[-1])}" for g in groups]


def months_between(first, last):
    out, m = [], first
    while m <= last:
        out.append(m)
        m = _month(m + dt.timedelta(days=32))
    return out


def statement_periods():
    """Each account's statement periods; uploads without printed dates fall back to their transactions."""
    periods = {}
    uploads = StatementUpload.objects.annotate(first=Min("transactions__date"), last=Max("transactions__date"))
    for s in uploads:
        lo, hi = s.period_start or s.first, s.period_end or s.last
        if lo and hi:
            periods.setdefault(s.account_id, []).append((lo, hi))
    return periods


def statement_grid(today):
    """One row per active bank/card account, one cell per complete month since it needs statements."""
    last = _month(_month(today) - dt.timedelta(days=1))
    months = months_between(START, last)
    periods = statement_periods()
    rows = []
    accounts = Account.objects.filter(is_active=True, kind__in=[Account.Kind.BANK, Account.Kind.CARD]).select_related("owner")
    for acc in sorted(accounts, key=lambda a: (a.kind, a.owner_id, a.id)):
        needed_from = _month(acc.statements_from) if acc.statements_from else START
        cells, gaps = [], []
        for m in months:
            if m < needed_from:
                cells.append({"state": "na", "title": f"{month_label(m)}: not needed"})
                continue
            total = _month_end(m).day
            got = days_covered(periods.get(acc.id, []), m, _month_end(m))
            state = OK if total - got <= GRACE_DAYS else (PARTIAL if got else MISSING)
            cells.append({"state": state, "title": f"{month_label(m)}: {got} of {total} days"})
            if state != OK:
                gaps.append(m)
        rows.append({"account": acc, "cells": cells, "gaps": gaps, "ranges": month_ranges(gaps),
                     "needed_from": needed_from if acc.statements_from else None})
    return {"months": months, "rows": rows}


def _statements_result(grid, kind, noun):
    rows = [r for r in grid["rows"] if r["account"].kind == kind]
    if not rows:
        return Result(MISSING, f"No {noun} accounts yet.", [(f"Add your {noun} accounts", "accounts")])
    gaps = [r for r in rows if r["gaps"]]
    if not gaps:
        return Result(OK, f"All {len(rows)} covered.")
    todos = [(f"{r['account']}: {', '.join(r['ranges'])}", "statements") for r in gaps]
    months = sum(len(r["gaps"]) for r in gaps)
    return Result(PARTIAL if len(gaps) < len(rows) or any(c["state"] == OK for r in rows for c in r["cells"]) else MISSING,
                  f"{months} account-month{'s' if months != 1 else ''} missing across {len(gaps)} of {len(rows)}.", todos)


def _worst(results):
    states = {r for r in results}
    if states == {OK}:
        return OK
    return MISSING if states == {MISSING} else PARTIAL


def _ledger_result(today):
    accounts = Account.objects.filter(is_active=True, kind=Account.Kind.BROKER, institution__iexact="Zerodha").select_related("owner")
    states, notes, todos = [], [], []
    for acc in accounts:
        last = CashFlow.objects.filter(account=acc, source=CashFlow.Source.KITE).aggregate(d=Max("date"))["d"]
        if not last:
            states.append(MISSING)
            todos.append((f"Zerodha ledger for {acc.owner.name}: from {month_label(START)} to now", "holdings"))
        elif (today - last).days > STALE_DAYS:
            states.append(PARTIAL)
            notes.append(f"{acc.owner.name}: last entry {last:%-d %b %Y}")
            todos.append((f"Zerodha ledger for {acc.owner.name}: since {month_label(last)}", "holdings"))
        else:
            states.append(OK)
    return Result(_worst(states) if states else MISSING, "; ".join(notes), todos)


def _pull_result(today, source, kind, label, todo_verb):
    accounts = Account.objects.filter(is_active=True, kind=kind).select_related("owner")
    if source == HoldingsPull.Source.KITE:
        accounts = accounts.filter(institution__iexact="Zerodha")
    states, notes, todos = [], [], []
    for acc in accounts:
        pull = HoldingsPull.objects.filter(account=acc, source=source).order_by("-as_of").first()
        if pull and (today - pull.as_of).days <= STALE_DAYS:
            states.append(OK)
            continue
        states.append(PARTIAL if pull else MISSING)
        if pull:
            notes.append(f"{acc.owner.name}: last {pull.as_of:%-d %b %Y}")
        todos.append((f"{todo_verb} {label} for {acc.owner.name}", "holdings"))
    return Result(_worst(states) if states else MISSING, "; ".join(notes), todos)


def _kite_result(today):
    result = _pull_result(today, HoldingsPull.Source.KITE, Account.Kind.BROKER, "Zerodha holdings", "Pull")
    unset = [p.name for p in Person.objects.people() if not kite.is_configured(p)
             and Account.objects.filter(owner=p, kind=Account.Kind.BROKER, is_active=True).exists()]
    if unset:
        result.detail = "; ".join(filter(None, [result.detail, f"Kite keys not in .env for {', '.join(unset)}"]))
    return result


def _monthend_result(today):
    latest = Snapshot.objects.order_by("-date").first()
    if not latest:
        return Result(MISSING, "No entries yet.", [("Fill in the month-end form", "snapshot_new")])
    if (today - latest.date).days > STALE_DAYS:
        return Result(PARTIAL, f"Last entry {latest.date:%-d %b %Y}.", [("Fill in the month-end form", "snapshot_new")])
    return Result(OK, f"Last entry {latest.date:%-d %b %Y}.")


def _loans_result():
    accounts = Account.objects.filter(kind=Account.Kind.LOAN, is_active=True).select_related("owner")
    bare = [a for a in accounts if not Loan.objects.filter(account=a).exists()]
    if not accounts:
        return Result(PARTIAL, "No loans added. Fine if you have none.")
    if bare:
        return Result(PARTIAL, f"{len(bare)} of {len(accounts)} without details.",
                      [(f"Add loan details for {a}", "loans") for a in bare])
    return Result(OK, f"All {len(accounts)} have details.")


def _checkpoint_result(today):
    loans = list(Loan.objects.select_related("account"))
    if not loans:
        return Result(PARTIAL, "No loans yet.")
    stale = []
    for loan in loans:
        last = LoanCheckpoint.objects.filter(loan=loan).aggregate(d=Max("date"))["d"]
        if not last or (today - last).days > 365:
            stale.append(loan)
    if not stale:
        return Result(OK, "Each loan was checked in the last year.")
    return Result(PARTIAL, f"{len(stale)} of {len(loans)} not checked in the last year.",
                  [(f"Enter the lender's outstanding for {x.account.name}", "loans") for x in stale])


def _values_result(today):
    accounts = list(Account.objects.filter(is_active=True, kind__in=[Account.Kind.FD, Account.Kind.ULIP]).select_related("owner"))
    if not accounts:
        return Result(OK, "No FD or ULIP accounts.")
    old = []
    for acc in accounts:
        last = Balance.objects.filter(account=acc).aggregate(d=Max("snapshot__date"))["d"]
        if not last or (today - last).days > STALE_DAYS:
            old.append(acc)
    if not old:
        return Result(OK, f"All {len(accounts)} have a recent value.")
    return Result(PARTIAL, f"{len(old)} of {len(accounts)} have no recent value.",
                  [(f"Update the value of {a}", "snapshot_new") for a in old])


def _reimburse_result():
    unpaired = Transaction.objects.filter(category=Category.REIMBURSEMENT, matched_with__isnull=True, amount__gt=0).count()
    if not unpaired:
        return Result(OK, "Every reimbursement is paired.")
    return Result(PARTIAL, f"{unpaired} reimbursement{'s' if unpaired != 1 else ''} with no matching purchase.",
                  [(f"Upload the card used for {unpaired} unpaired reimbursement{'s' if unpaired != 1 else ''}", "statements")])


def _plan_result():
    gaps = []
    if not Household.get().monthly_net_income:
        gaps.append("net income")
    if not MonthlyExpense.objects.exists():
        gaps.append("monthly expenses")
    if not AllocationTarget.objects.exists():
        gaps.append("target allocation")
    if not gaps:
        return Result(OK, "All filled in.")
    return Result(PARTIAL, f"Missing: {', '.join(gaps)}.", [(f"Set {g} on the Plan page", "plan") for g in gaps])


def run_check(today=None):
    today = today or dt.date.today()
    grid = statement_grid(today)
    results = {
        "bank": _statements_result(grid, Account.Kind.BANK, "bank"),
        "card": _statements_result(grid, Account.Kind.CARD, "credit card"),
        "ledger": _ledger_result(today),
        "kite": _kite_result(today),
        "cas": _pull_result(today, HoldingsPull.Source.CAS, Account.Kind.MF, "a CAS", "Upload"),
        "monthend": _monthend_result(today),
        "loans": _loans_result(),
        "checkpoint": _checkpoint_result(today),
        "values": _values_result(today),
        "reimburse": _reimburse_result(),
        "plan": _plan_result(),
    }
    return grid, results


def _todo_list(items, results):
    return [{"text": text, "url": reverse(url), "group": item["title"]}
            for item in items for text, url in results[item["key"]].todos]


@login_required
def checklist(request):
    checked = "check" in request.GET
    grid = results = None
    if checked:
        grid, results = run_check()
    sections = []
    for key, title in (("core", "Core"), ("extra", "Good to have")):
        rows = []
        for item in ITEMS[key]:
            r = results[item["key"]] if results else None
            rows.append({**item, "href": reverse(item["url"]), "status": r.status if r else None,
                         "status_label": STATUS_LABELS[r.status] if r else "", "detail": r.detail if r else ""})
        sections.append({"key": key, "title": title, "items": rows})
    context = {"sections": sections, "checked": checked, "grid": grid}
    if results:
        context["core_todos"] = _todo_list(ITEMS["core"], results)
        context["extra_todos"] = _todo_list(ITEMS["extra"], results)
        context["unconfirmed"] = Transaction.objects.filter(confirmed=False).count()
        context["start"] = START
        context["month_options"] = grid["months"]
    return render(request, "core/checklist.html", context)


@login_required
@require_POST
def statements_from(request, pk):
    acc = get_object_or_404(Account, pk=pk, kind__in=[Account.Kind.BANK, Account.Kind.CARD])
    value = request.POST.get("statements_from", "")
    try:
        acc.statements_from = dt.date.fromisoformat(value) if value else None
    except ValueError:
        messages.error(request, "That month wasn't recognised.")
        return redirect(reverse("checklist") + "?check=1#months")
    acc.save(update_fields=["statements_from"])
    return redirect(reverse("checklist") + "?check=1#months")
