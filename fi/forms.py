import datetime as dt
from decimal import Decimal

from django import forms

from core.models import AssetClass, Person

CLASSES = [c for c in AssetClass.values if c != AssetClass.LIABILITY]
H = {"class": "form-control form-control-sm"}


def pct_field(label, required=False, help_text=""):
    return forms.DecimalField(label=label, required=required, max_digits=6, decimal_places=2,
                              help_text=help_text, widget=forms.NumberInput(attrs={**H, "step": "0.01"}))


def money_field(label, required=False, help_text=""):
    return forms.DecimalField(label=label, required=required, max_digits=14, decimal_places=2,
                              help_text=help_text, widget=forms.NumberInput(attrs={**H, "step": "1"}))


class PlanForm(forms.Form):
    # Spending
    monthly_expenses = money_field("Monthly expenses (₹)", True, "Rough figure for everything you spend in a month.")
    includes_emi = forms.BooleanField(label="This figure includes the home-loan EMI", required=False, initial=True)
    expenses_from = forms.DateField(label="Applies from", widget=forms.DateInput(attrs={**H, "type": "date"}))
    # Income / life energy
    monthly_net_income = money_field("Household take-home income per month (₹)")
    hours_worked_per_month = forms.DecimalField(label="Hours given to work per month", max_digits=6, decimal_places=1,
                                                help_text="Include commute and work-related time (YMOYL).",
                                                widget=forms.NumberInput(attrs=H))
    work_costs_monthly = money_field("Work-related costs per month (₹)", help_text="Commute, work clothes, meals out because of work…")
    planned_monthly_investment = money_field("Planned new investment per month (₹)", help_text="Used until 3 months of actual investing are tracked.")
    # FI assumptions
    safe_withdrawal_rate = pct_field("Safe withdrawal rate (%)", True, "3.33% = corpus of 30× annual expenses.")
    inflation = pct_field("Inflation (%)", True)
    emergency_fund_months = forms.IntegerField(label="Emergency fund (months of expenses)", min_value=0, widget=forms.NumberInput(attrs=H))
    target_fi_date = forms.DateField(label="Target FI date (optional)", required=False, widget=forms.DateInput(attrs={**H, "type": "date"}))
    # Home loan
    loan_owner = forms.ModelChoiceField(label="Loan in whose name", queryset=Person.objects.all(), required=False,
                                        widget=forms.Select(attrs={"class": "form-select form-select-sm"}))
    loan_outstanding = money_field("Outstanding today (₹)")
    loan_emi = money_field("EMI (₹)")
    loan_rate = pct_field("Interest rate (%)")
    loan_end_date = forms.DateField(label="Ends", required=False, widget=forms.DateInput(attrs={**H, "type": "date"}))
    loan_backfill = forms.BooleanField(label="Estimate the outstanding for past months too (from EMI and rate)",
                                       required=False, initial=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for c in CLASSES:
            label = AssetClass(c).label
            self.fields[f"target_{c}"] = pct_field(f"{label} target %")
            self.fields[f"band_{c}"] = pct_field(f"{label} band ±%")
            self.fields[f"return_{c}"] = pct_field(f"{label} expected return %")
        self.fields["includes_emi"].widget.attrs["class"] = "form-check-input"
        self.fields["loan_backfill"].widget.attrs["class"] = "form-check-input"

    def clean(self):
        data = super().clean()
        targets = [data.get(f"target_{c}") for c in CLASSES]
        if any(t is not None for t in targets) and abs(sum(t or 0 for t in targets) - 100) > Decimal("0.5"):
            raise forms.ValidationError("Allocation targets must add up to 100%.")
        if data.get("loan_outstanding") and not data.get("loan_owner"):
            self.add_error("loan_owner", "Choose whose name the loan is in.")
        return data

    def allocation_rows(self):
        return [{"label": AssetClass(c).label, "target": self[f"target_{c}"], "band": self[f"band_{c}"],
                 "ret": self[f"return_{c}"]} for c in CLASSES]
