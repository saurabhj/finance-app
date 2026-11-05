"""Accounts page: list every account, add new ones, edit a row in place."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Case, IntegerField, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import Account, AssetClass, Person

DEFAULT_ASSET_CLASS = {
    "bank": AssetClass.CASH, "card": AssetClass.LIABILITY, "loan": AssetClass.LIABILITY,
    "fd": AssetClass.DEBT, "broker": AssetClass.EQUITY, "mf": AssetClass.EQUITY,
    "ulip": AssetClass.HYBRID, "other": AssetClass.EQUITY, "property": AssetClass.HYBRID,
}
KIND_ORDER = ["bank", "card", "fd", "broker", "mf", "ulip", "other", "property", "loan"]


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ["owner", "kind", "name", "institution", "match_hints"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["owner"].queryset = Person.objects.all()
        self.fields["owner"].empty_label = "Owner"
        self.fields["kind"].choices = [("", "Type")] + [(k, Account.Kind(k).label) for k in KIND_ORDER]
        placeholders = {"name": "Name, e.g. HDFC Regalia", "institution": "Bank / provider",
                        "match_hints": "Numbers on its statements"}
        for name, f in self.fields.items():
            f.widget.attrs["class"] = "form-select form-select-sm" if isinstance(f.widget, forms.Select) else "form-control form-control-sm"
            if name in placeholders:
                f.widget.attrs["placeholder"] = placeholders[name]


def _ordered(qs):
    order = Case(*[When(kind=k, then=Value(i)) for i, k in enumerate(KIND_ORDER)], output_field=IntegerField())
    return qs.select_related("owner").annotate(kind_order=order).order_by("-is_active", "kind_order", "owner_id", "sort_order", "id")


def _form_errors(form):
    return "; ".join(f"{form.fields[k].label if k in form.fields else ''}: {v[0]}".lstrip(": ") for k, v in form.errors.items())


@login_required
def accounts(request):
    form = AccountForm()
    return render(request, "core/accounts.html", {
        "accounts": _ordered(Account.objects.all()),
        "form": form,
    })


@login_required
@require_POST
def account_add(request):
    form = AccountForm(request.POST)
    if form.is_valid():
        acc = form.save(commit=False)
        acc.asset_class = DEFAULT_ASSET_CLASS.get(acc.kind, AssetClass.EQUITY)
        acc.save()
        messages.success(request, f"Added {acc}.")
        if acc.kind == Account.Kind.LOAN:
            return redirect(reverse("loan_new") + f"?account={acc.id}")
        return redirect(reverse("accounts") + f"#acc-{acc.id}")
    messages.error(request, "Couldn't add that account. " + _form_errors(form))
    return redirect("accounts")


@login_required
@require_POST
def account_edit(request, pk):
    acc = get_object_or_404(Account, pk=pk)
    name = request.POST.get("name", "").strip()[:80]
    if not name:
        messages.error(request, "An account needs a name.")
        return redirect(reverse("accounts") + f"#acc-{acc.id}")
    if Account.objects.filter(owner=acc.owner, name__iexact=name).exclude(pk=acc.pk).exists():
        messages.error(request, f"{acc.owner.name} already has an account called “{name}”.")
        return redirect(reverse("accounts") + f"#acc-{acc.id}")
    acc.name = name
    acc.institution = request.POST.get("institution", "").strip()[:80]
    acc.match_hints = request.POST.get("match_hints", "").strip()[:200]
    acc.is_active = request.POST.get("is_active") == "on"
    acc.save(update_fields=["name", "institution", "match_hints", "is_active"])
    messages.success(request, f"Saved {acc}.")
    return redirect(reverse("accounts") + f"#acc-{acc.id}")
