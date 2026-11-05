import datetime as dt
import json

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction as db_tx
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ai.review import ReviewError
from core.models import (Account, Category, CategoryRule, Household, SpendCategory, StatementUpload,
                         Transaction, UploadBatch)

from . import batch as batches
from . import importer, matching, rules, spending

MAX_UPLOAD = 25 * 1024 * 1024


class RuleForm(forms.ModelForm):
    class Meta:
        model = CategoryRule
        fields = ["keyword", "applies_to", "direction", "category", "spend_category", "target_account", "party"]
        labels = {"target_account": "Investment account (for investments)", "applies_to": "Applies to",
                  "spend_category": "Spend type (for spending)"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["target_account"].queryset = Account.investment_accounts().filter(is_active=True)
        for f in self.fields.values():
            f.widget.attrs["class"] = "form-select form-select-sm" if isinstance(f.widget, forms.Select) else "form-control form-control-sm"

    def clean(self):
        d = super().clean()
        if d.get("category") == Category.EXPENSE and not d.get("spend_category"):
            self.add_error("spend_category", "Pick a spend type for spending rules.")
        if d.get("category") in (Category.INVESTMENT, Category.INVESTMENT_RETURN) and not d.get("target_account"):
            self.add_error("target_account", "Pick the investment account.")
        if d.get("keyword"):
            d["keyword"] = d["keyword"].strip().upper()
        return d


def _statement_accounts():
    return Account.objects.filter(is_active=True, kind__in=[Account.Kind.BANK, Account.Kind.CARD]).select_related("owner")


@login_required
def home(request):
    return render(request, "core/statements.html", {
        "accounts": _statement_accounts(),
        "uploads": StatementUpload.objects.select_related("account__owner")[:40],
        "unconfirmed": Transaction.objects.filter(confirmed=False).count(),
        "user_rules": CategoryRule.objects.filter(is_builtin=False).select_related("target_account"),
        "batches": UploadBatch.objects.all()[:8],
        "rule_form": RuleForm(),
        "notes": Household.get().categorization_notes,
    })


@login_required
@require_POST
def add_rule(request):
    form = RuleForm(request.POST)
    if form.is_valid():
        rule = form.save()
        n = importer.reapply_rules()
        messages.success(request, f'Rule "{rule.keyword}" added; {n} unconfirmed transaction{"s" if n != 1 else ""} re-tagged.')
    else:
        messages.error(request, "Rule not added: " + "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    return redirect("statements")


@login_required
@require_POST
def save_notes(request):
    hh = Household.get()
    hh.categorization_notes = request.POST.get("notes", "")[:5000]
    hh.save(update_fields=["categorization_notes"])
    messages.success(request, "Notes saved. Claude will follow them for new uploads and reviews.")
    return redirect("statements")


@login_required
@require_POST
def reapply(request):
    n = importer.reapply_rules()
    messages.success(request, f"{n} unconfirmed transaction{'s' if n != 1 else ''} re-tagged by rules.")
    return redirect("statements_review")


@login_required
@require_POST
def upload(request):
    """Bulk upload: any number of files (or ZIPs) across accounts, processed in the background."""
    files = request.FILES.getlist("files")
    if not files:
        messages.error(request, "Choose at least one file.")
        return redirect("statements")
    too_big = [f.name for f in files if f.size > MAX_UPLOAD]
    if too_big:
        messages.error(request, "Larger than 25 MB, skipped: " + ", ".join(too_big))
    account = None
    if request.POST.get("account"):
        account = get_object_or_404(Account, pk=request.POST["account"], kind__in=["bank", "card"])
    b = batches.create_batch(((f.name, f.read()) for f in files if f.size <= MAX_UPLOAD),
                             request.POST.get("passwords", ""), account)
    if not b.items.exists():
        messages.error(request, "No statement files found (PDF, CSV, XLS, XLSX or ZIP).")
        b.delete()
        return redirect("statements")
    if b.items.filter(status="queued", account__isnull=False).exists():
        batches.start_worker(b)
    else:
        batches.finish(b)
    return redirect("statement_batch", pk=b.pk)


@login_required
def batch_detail(request, pk):
    b = get_object_or_404(UploadBatch, pk=pk)
    items = b.items.select_related("account__owner", "statement")
    active = items.filter(status__in=["queued", "processing"], account__isnull=False).exists()
    return render(request, "core/statement_batch.html", {
        "batch": b, "items": items, "counts": b.counts(), "active": active,
        "accounts": _statement_accounts(),
    })


@login_required
@require_POST
def batch_assign(request, pk):
    """Assign accounts to files that couldn't be matched, then process them."""
    b = get_object_or_404(UploadBatch, pk=pk)
    assigned = 0
    for item in b.items.filter(status="needs_account"):
        acc_id = request.POST.get(f"account_{item.id}")
        if acc_id:
            item.account = get_object_or_404(Account, pk=acc_id, kind__in=["bank", "card"])
            item.status, item.detected_by, item.message = "queued", "chosen by you", ""
            item.save()
            assigned += 1
            if request.POST.get(f"remember_{item.id}") and request.POST.get(f"hint_{item.id}", "").strip():
                hint = request.POST[f"hint_{item.id}"].strip()
                acc = item.account
                acc.match_hints = ", ".join(h for h in [acc.match_hints, hint] if h)
                acc.save(update_fields=["match_hints"])
    if request.POST.get("skip_rest"):
        b.items.filter(status="needs_account").update(status="failed", message="Skipped")
    if assigned:
        b.status = "running"
        b.save(update_fields=["status"])
        batches.start_worker(b)
    else:
        batches.finish(b)
    return redirect("statement_batch", pk=b.pk)


@login_required
@require_POST
def delete_upload(request, pk):
    stmt = get_object_or_404(StatementUpload, pk=pk)
    with db_tx.atomic():
        for t in stmt.transactions.exclude(cash_flow=None):
            matching._unlink_flow(t)
        stmt.delete()
    messages.info(request, "Statement and its transactions deleted.")
    return redirect("statements")


@login_required
@require_POST
def delete_rule(request, pk):
    get_object_or_404(CategoryRule, pk=pk, is_builtin=False).delete()
    return redirect("statements")


@login_required
def review(request):
    status = request.GET.get("status", "unconfirmed")
    qs = Transaction.objects.select_related("account__owner", "investment_account", "matched_with__account")
    if status == "unconfirmed":
        qs = qs.filter(confirmed=False)
    if request.GET.get("account"):
        qs = qs.filter(account_id=request.GET["account"])
    if request.GET.get("category") is not None and request.GET.get("category") != "all":
        qs = qs.filter(category=request.GET["category"])
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(_search(q))
    sort = request.GET.get("sort", "-date")
    if sort.lstrip("-") not in REVIEW_SORTS:
        sort = "-date"
    field = REVIEW_SORTS[sort.lstrip("-")]
    order = [f"-{f}" if sort.startswith("-") else f for f in field] + ["id"]
    page = Paginator(qs.order_by(*order), 150).get_page(request.GET.get("page"))
    for t in page:
        t.keyword_guess = rules.keyword_guess(t.description)
    return render(request, "core/statement_review.html", {
        **_row_choices(), "page": page, "status": status, "accounts": _statement_accounts(),
        "filter_account": request.GET.get("account", ""), "filter_category": request.GET.get("category", "all"),
        "q": q, "sort": sort, "sort_links": _sort_links(request, sort),
        "page_query": _without_page(request),
    })


def _row_choices():
    """What the editable row in core/includes/review_row.html needs."""
    return {"categories": Category.choices, "spend_categories": SpendCategory.choices,
            "investment_accounts": Account.investment_accounts().filter(is_active=True).select_related("owner")}


def _without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


# column key -> model fields it orders by
REVIEW_SORTS = {"date": ["date"], "account": ["account__name"], "description": ["description"],
                "amount": ["amount"], "category": ["category"]}


def _search(q):
    """Every word must appear in the description, party or account name; a number also matches the amount."""
    from decimal import Decimal, InvalidOperation

    from django.db.models import Q
    cond = Q()
    for word in q.split():
        w = Q(description__icontains=word) | Q(party__icontains=word) | Q(account__name__icontains=word)
        try:
            n = Decimal(word.replace(",", "").replace("₹", ""))
            w |= Q(amount=n) | Q(amount=-n)
        except InvalidOperation:
            pass
        cond &= w
    return cond


def _sort_links(request, current):
    """Header link for each sortable column: same filters, first page, direction toggled when already sorted."""
    links = {}
    for key in REVIEW_SORTS:
        params = request.GET.copy()
        params.pop("page", None)
        default_desc = key in ("date", "amount")
        if current.lstrip("-") == key:
            params["sort"] = key if current.startswith("-") else f"-{key}"
            arrow = "↓" if current.startswith("-") else "↑"
        else:
            params["sort"] = f"-{key}" if default_desc else key
            arrow = ""
        links[key] = {"url": "?" + params.urlencode(), "arrow": arrow}
    return links


@login_required
@require_POST
def review_save(request):
    ids = request.POST.getlist("txn")
    confirmed = skipped = taught = 0
    inv_accounts = {a.id: a for a in Account.investment_accounts()}
    for t in Transaction.objects.filter(id__in=ids).select_related("account"):
        cat = request.POST.get(f"cat_{t.id}", t.category)
        spend = request.POST.get(f"spend_{t.id}", "") if cat == Category.EXPENSE else ""
        party = request.POST.get(f"party_{t.id}", t.party).strip()[:60] if cat in (Category.REIMBURSABLE, Category.REIMBURSEMENT) else ""
        if party != t.party:
            t.party = party
            t.save(update_fields=["party"])
        inv = request.POST.get(f"inv_{t.id}") or None
        inv_acc = inv_accounts.get(int(inv)) if inv else None
        if (cat, spend, inv_acc and inv_acc.id) != (t.category, t.spend_category, t.investment_account_id):
            t.category, t.spend_category, t.source = cat, spend, Transaction.Source.USER
            t.investment_account = inv_acc if cat in (Category.INVESTMENT, Category.INVESTMENT_RETURN) else None
            t.save()
            if t.confirmed:  # editing a confirmed row re-confirms it so its cash flow follows
                matching.confirm(t)
        if request.POST.get(f"rem_{t.id}"):
            taught += _remember(t, request.POST.get(f"kw_{t.id}", "").strip())
        if request.POST.get(f"ok_{t.id}") and not t.confirmed:
            if t.category in (Category.INVESTMENT, Category.INVESTMENT_RETURN) and not t.investment_account_id:
                skipped += 1
                continue
            if not t.category:
                skipped += 1
                continue
            matching.confirm(t)
            confirmed += 1
    from .reimburse import pair_reimbursements
    paired = pair_reimbursements()
    parts = [f"{confirmed} confirmed"]
    if paired:
        parts.append(f"{paired} reimbursement{'s' if paired > 1 else ''} matched to purchases")
    if taught:
        parts.append(f"{taught} rule{'s' if taught > 1 else ''} learned")
    if skipped:
        parts.append(f"{skipped} skipped (need a category, and an account for investments)")
    messages.success(request, ", ".join(parts) + ".")
    return redirect(request.POST.get("next") or "statements_review")


def _remember(t, keyword):
    if len(keyword) < 3:
        return 0
    rule = CategoryRule.objects.create(
        keyword=keyword.upper()[:100], applies_to="card" if t.account.kind == "card" else "bank",
        direction="credit" if t.amount > 0 else "debit", category=t.category,
        spend_category=t.spend_category, target_account=t.investment_account, party=t.party,
    )
    # apply to other unconfirmed rows right away
    for other in Transaction.objects.filter(confirmed=False).exclude(pk=t.pk).select_related("account"):
        if rules.match_rule([rule], other.description, other.amount, other.account):
            rules.apply_rule(other, rule)
            other.save()
    return 1


@login_required
def spending_view(request):
    confirmed_only = request.GET.get("confirmed") == "1"
    rows = spending.monthly(confirmed_only=confirmed_only)
    sugg = spending.suggestion(rows)
    cats = []
    for r in rows:
        for c in r["by_cat"]:
            if c not in cats:
                cats.append(c)
    chart = {"labels": [r["month"].strftime("%b %Y") for r in rows],
             "series": {c: [float(r["by_cat"].get(c, 0)) for r in rows] for c in cats},
             "emi": [float(r["emi"]) for r in rows],
             "links": [reverse("spending_month", args=[r["month"].year, r["month"].month]) for r in rows]}
    from .reimburse import summary as reimb_summary
    return render(request, "core/spending.html", {"rows": list(reversed(rows)), "suggestion": sugg,
                                                  "reimb": reimb_summary(), "home_total": spending.home_total(),
                                                  "chart_json": json.dumps(chart), "confirmed_only": confirmed_only})


@login_required
def spending_month(request, year, month):
    try:
        start = dt.date(year, month, 1)
    except ValueError:
        raise Http404("No such month")
    unconfirmed_only = request.GET.get("show") == "unconfirmed"
    by_date = request.GET.get("sort") == "date"
    detail = spending.month_detail(start, unconfirmed_only, by_date)
    for g in detail["groups"]:
        for t in g["rows"]:
            t.keyword_guess = rules.keyword_guess(t.description)
    prev_month, next_month = spending.month_neighbours(start)
    return render(request, "core/spending_month.html", {
        **_row_choices(), **detail, "month": start, "prev_month": prev_month, "next_month": next_month,
        "unconfirmed_only": unconfirmed_only, "by_date": by_date,
    })


@login_required
@require_POST
def spending_adopt(request):
    rows = spending.monthly()
    sugg = spending.suggestion(rows)
    if sugg:
        spending.adopt(sugg["amount"], sugg["include_emi"])
        messages.success(request, f"Plan updated: monthly expenses ₹{sugg['amount']:,.0f}.")
    return redirect("fi")
