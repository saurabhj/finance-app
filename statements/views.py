import json

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction as db_tx
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ai.review import ReviewError
from core.models import (Account, AssetClass, Category, CategoryRule, Household, SpendCategory, StatementUpload,
                         Transaction)

from . import importer, matching, rules, spending

MAX_UPLOAD = 25 * 1024 * 1024


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ["owner", "name", "institution", "kind"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["kind"].choices = [("bank", "Bank account"), ("card", "Credit card")]
        for f in self.fields.values():
            f.widget.attrs["class"] = "form-select form-select-sm" if isinstance(f.widget, forms.Select) else "form-control form-control-sm"


class RuleForm(forms.ModelForm):
    class Meta:
        model = CategoryRule
        fields = ["keyword", "applies_to", "direction", "category", "spend_category", "target_account"]
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
        "account_form": AccountForm(),
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
def add_account(request):
    form = AccountForm(request.POST)
    if form.is_valid():
        acc = form.save(commit=False)
        acc.asset_class = AssetClass.CASH if acc.kind == "bank" else AssetClass.LIABILITY
        acc.save()
        messages.success(request, f"Added {acc}.")
    else:
        messages.error(request, "Couldn't add that account: " + "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    return redirect("statements")


@login_required
@require_POST
def upload(request):
    account = get_object_or_404(Account, pk=request.POST.get("account"), kind__in=["bank", "card"])
    files = request.FILES.getlist("files")
    if not files:
        messages.error(request, "Choose at least one file.")
        return redirect("statements")
    for f in files:
        if f.size > MAX_UPLOAD:
            messages.error(request, f"{f.name}: larger than 25 MB.")
            continue
        try:
            stmt = importer.import_statement(account, f.name, f.read(), request.POST.get("password", ""))
        except (importer.ImportError_, ValueError, ReviewError) as e:
            messages.error(request, f"{f.name}: {e}")
            continue
        msg = f"{f.name}: {stmt.added} transactions added"
        if stmt.duplicates:
            msg += f", {stmt.duplicates} already known"
        messages.success(request, msg + ".")
        if stmt.warning:
            messages.warning(request, f"{f.name}: {stmt.warning}")
    return redirect("statements_review")


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
    page = Paginator(qs.order_by("-date", "id"), 150).get_page(request.GET.get("page"))
    for t in page:
        t.keyword_guess = rules.keyword_guess(t.description)
    return render(request, "core/statement_review.html", {
        "page": page, "status": status, "accounts": _statement_accounts(),
        "categories": Category.choices, "spend_categories": SpendCategory.choices,
        "investment_accounts": Account.investment_accounts().filter(is_active=True).select_related("owner"),
        "filter_account": request.GET.get("account", ""), "filter_category": request.GET.get("category", "all"),
    })


@login_required
@require_POST
def review_save(request):
    ids = request.POST.getlist("txn")
    confirmed = skipped = taught = 0
    inv_accounts = {a.id: a for a in Account.investment_accounts()}
    for t in Transaction.objects.filter(id__in=ids).select_related("account"):
        cat = request.POST.get(f"cat_{t.id}", t.category)
        spend = request.POST.get(f"spend_{t.id}", "") if cat == Category.EXPENSE else ""
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
    parts = [f"{confirmed} confirmed"]
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
        spend_category=t.spend_category, target_account=t.investment_account,
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
             "emi": [float(r["emi"]) for r in rows]}
    return render(request, "core/spending.html", {"rows": list(reversed(rows)), "suggestion": sugg,
                                                  "chart_json": json.dumps(chart), "confirmed_only": confirmed_only})


@login_required
@require_POST
def spending_adopt(request):
    rows = spending.monthly()
    sugg = spending.suggestion(rows)
    if sugg:
        spending.adopt(sugg["amount"], sugg["include_emi"])
        messages.success(request, f"Plan updated: monthly expenses ₹{sugg['amount']:,.0f}.")
    return redirect("fi")
