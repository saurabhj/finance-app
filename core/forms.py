from decimal import Decimal

from django import forms

from .models import Account, CashFlow


class SnapshotForm(forms.Form):
    """Month-end entry: one value per account, plus money invested/withdrawn
    for each investment account since the previous snapshot."""

    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, accounts, initial_values=None, initial_flows=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.accounts = list(accounts)
        initial_values = initial_values or {}
        initial_flows = initial_flows or {}
        for acc in self.accounts:
            self.fields[f"value_{acc.id}"] = forms.DecimalField(
                required=False, max_digits=14, decimal_places=2,
                initial=initial_values.get(acc.id),
                label=acc.name,
            )
            if acc.is_investment:
                self.fields[f"flow_{acc.id}"] = forms.DecimalField(
                    required=False, max_digits=14, decimal_places=2,
                    initial=initial_flows.get(acc.id),
                    label="Invested (+) / withdrawn (−)",
                )

    def rows(self):
        """Accounts grouped by owner for the template."""
        groups = {}
        for acc in self.accounts:
            groups.setdefault(acc.owner.name, []).append({
                "account": acc,
                "value": self[f"value_{acc.id}"],
                "flow": self[f"flow_{acc.id}"] if acc.is_investment else None,
            })
        return groups

    def values(self):
        return {
            acc.id: self.cleaned_data.get(f"value_{acc.id}")
            for acc in self.accounts
            if self.cleaned_data.get(f"value_{acc.id}") is not None
        }

    def flows(self):
        out = {}
        for acc in self.accounts:
            v = self.cleaned_data.get(f"flow_{acc.id}")
            if acc.is_investment and v not in (None, Decimal("0")):
                out[acc.id] = v
        return out


class CashFlowForm(forms.ModelForm):
    class Meta:
        model = CashFlow
        fields = ["date", "account", "amount", "kind", "from_account", "note"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}
        help_texts = {"amount": "Positive = money put in, negative = money taken out."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        accounts = Account.objects.filter(is_active=True).select_related("owner")
        self.fields["account"].queryset = accounts.exclude(kind__in=Account.NON_INVESTMENT_KINDS)
        self.fields["from_account"].queryset = accounts
        for f in self.fields.values():
            css = "form-select" if isinstance(f.widget, forms.Select) else "form-control"
            f.widget.attrs.setdefault("class", f"{css} form-control-sm")
