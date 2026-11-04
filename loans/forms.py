from decimal import Decimal

from django import forms

from core.models import Loan, LoanCheckpoint, LoanPrepayment, LoanRate, Person

DATE = {"type": "date"}


def style(form):
    for f in form.fields.values():
        if isinstance(f.widget, forms.CheckboxInput):
            continue
        cls = "form-select form-select-sm" if isinstance(f.widget, forms.Select) else "form-control form-control-sm"
        f.widget.attrs.setdefault("class", cls)


class PercentField(forms.DecimalField):
    """Shown and typed as a percentage (7.6), stored as a fraction (0.076)."""

    def __init__(self, **kw):
        super().__init__(max_digits=6, decimal_places=3, **kw)

    def prepare_value(self, value):
        if isinstance(value, Decimal) and value < 1:
            return (value * 100).normalize()
        return value

    def clean(self, value):
        v = super().clean(value)
        return (v / 100).quantize(Decimal("0.0001")) if v is not None else None


class LoanForm(forms.ModelForm):
    name = forms.CharField(max_length=80, help_text="e.g. Home loan, Car loan")
    owner = forms.ModelChoiceField(queryset=Person.objects.all(), label="In whose name",
                                   help_text="Choose Joint for loans in both your names")
    initial_rate = PercentField(label="Interest rate at start (%)", required=False,
                                help_text="Later changes go in the rate history on the loan page")

    class Meta:
        model = Loan
        fields = ["lender", "loan_type", "rate_type", "principal", "start_date", "first_emi_date",
                  "tenure_months", "emi", "statement_keyword", "notes", "closed"]
        widgets = {"start_date": forms.DateInput(attrs=DATE), "first_emi_date": forms.DateInput(attrs=DATE),
                   "notes": forms.Textarea(attrs={"rows": 2})}
        labels = {"principal": "Amount (₹)", "tenure_months": "Tenure (months)", "emi": "EMI (₹)",
                  "closed": "Loan closed"}

    def __init__(self, *a, account=None, **kw):
        self.account = account
        super().__init__(*a, **kw)
        if account:
            self.fields["name"].initial = account.name
            self.fields["owner"].initial = account.owner_id
        if self.instance.pk:
            del self.fields["initial_rate"]
        else:
            self.fields["initial_rate"].required = True
        style(self)


class RateForm(forms.ModelForm):
    rate = PercentField(label="New rate (%)")

    class Meta:
        model = LoanRate
        fields = ["effective_date", "rate", "new_emi", "note"]
        widgets = {"effective_date": forms.DateInput(attrs=DATE)}
        labels = {"new_emi": "New EMI (₹, if the bank changed it)"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        style(self)


class PrepaymentForm(forms.ModelForm):
    class Meta:
        model = LoanPrepayment
        fields = ["date", "amount", "effect", "note"]
        widgets = {"date": forms.DateInput(attrs=DATE)}
        labels = {"amount": "Amount (₹)"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        style(self)


class CheckpointForm(forms.ModelForm):
    class Meta:
        model = LoanCheckpoint
        fields = ["date", "outstanding", "note"]
        widgets = {"date": forms.DateInput(attrs=DATE)}
        labels = {"outstanding": "Outstanding per bank (₹)"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        style(self)
