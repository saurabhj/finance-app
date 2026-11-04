from decimal import Decimal

from django.db import models


class AssetClass(models.TextChoices):
    EQUITY = "equity", "Equity"
    GOLD = "gold", "Gold"
    DEBT = "debt", "Debt"
    CASH = "cash", "Cash"
    HYBRID = "hybrid", "Hybrid"
    LIABILITY = "liability", "Liability"


class Person(models.Model):
    name = models.CharField(max_length=50, unique=True)
    user = models.OneToOneField(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class Account(models.Model):
    """One place money sits: a bank account, a demat, an FD bundle, a loan."""

    class Kind(models.TextChoices):
        BANK = "bank", "Bank account"
        FD = "fd", "Fixed deposit"
        BROKER = "broker", "Broker / demat"
        MF = "mf", "Mutual funds"
        ULIP = "ulip", "ULIP / insurance"
        OTHER = "other", "Other investment"
        LOAN = "loan", "Loan"

    owner = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="accounts")
    name = models.CharField(max_length=80)
    institution = models.CharField(max_length=80, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    asset_class = models.CharField(
        max_length=10, choices=AssetClass.choices, default=AssetClass.EQUITY,
        help_text="Default asset class when holdings are not broken down.",
    )
    expected_return = models.DecimalField(
        max_digits=6, decimal_places=4, default=Decimal("0"),
        help_text="Assumed annual return (0.12 = 12%), carried over from the sheet.",
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    # Loans only
    emi = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    interest_rate = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True,
                                        help_text="Annual, e.g. 0.085")
    loan_end_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["owner_id", "sort_order", "id"]
        unique_together = [("owner", "name")]

    def __str__(self):
        return f"{self.owner.name} · {self.name}"

    @property
    def is_investment(self):
        return self.kind not in (self.Kind.BANK, self.Kind.LOAN)

    @property
    def is_liability(self):
        return self.kind == self.Kind.LOAN


class Snapshot(models.Model):
    """Values of every account on one date (normally month end, before the EMI)."""

    class Source(models.TextChoices):
        IMPORT = "import", "Imported from sheet"
        MANUAL = "manual", "Manual entry"
        KITE = "kite", "Zerodha Kite"
        CAS = "cas", "CAS statement"

    date = models.DateField(unique=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    note = models.TextField(blank=True)
    flows_tracked = models.BooleanField(
        default=True,
        help_text="False for history imported from the sheet, where money invested was not recorded.",
    )

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return f"Snapshot {self.date}"


class Balance(models.Model):
    snapshot = models.ForeignKey(Snapshot, on_delete=models.CASCADE, related_name="balances")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="balances")
    value = models.DecimalField(max_digits=14, decimal_places=2)
    pull = models.ForeignKey(
        "HoldingsPull", null=True, blank=True, on_delete=models.SET_NULL, related_name="balances",
        help_text="Holdings pull (Kite/CAS) this value came from, for the holding-level breakdown.",
    )

    class Meta:
        unique_together = [("snapshot", "account")]

    def __str__(self):
        return f"{self.account} @ {self.snapshot.date}: {self.value}"


class CashFlow(models.Model):
    """Money put into (positive) or taken out of (negative) an investment account.

    Market gain for a period = closing - opening - sum(cash flows in the period).
    A switch (e.g. FD matured -> Zerodha) is two rows: -X on the FD, +X on Zerodha,
    so it nets to zero at family level.
    """

    class Kind(models.TextChoices):
        INVEST = "invest", "Invested (new money)"
        WITHDRAW = "withdraw", "Withdrawn to bank"
        SWITCH = "switch", "Switch between investments"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        KITE = "kite", "Zerodha ledger"
        CAS = "cas", "CAS statement"

    date = models.DateField()
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="cash_flows")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.INVEST)
    from_account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Where the money came from (informational).",
    )
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    note = models.CharField(max_length=200, blank=True)
    instrument = models.ForeignKey("Instrument", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="cash_flows", help_text="Fund this flow bought/sold (CAS).")
    external_id = models.CharField(
        max_length=100, null=True, blank=True, unique=True,
        help_text="Dedupe key for imported rows (ledger / CAS), so re-uploading is safe.",
    )

    class Meta:
        ordering = ["-date", "id"]

    def __str__(self):
        return f"{self.date} {self.account}: {self.amount}"


class Instrument(models.Model):
    """A stock, ETF, SGB or mutual fund scheme held inside an account."""

    class Kind(models.TextChoices):
        STOCK = "stock", "Stock"
        ETF = "etf", "ETF"
        SGB = "sgb", "Sovereign Gold Bond"
        MF = "mf", "Mutual fund"
        OTHER = "other", "Other"

    class Management(models.TextChoices):
        ADVISOR = "advisor", "Advisor-managed"
        SELF = "self", "Self-managed (Claude reviews)"

    identifier = models.CharField(max_length=40, unique=True, help_text="Trading symbol or ISIN")
    name = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.STOCK)
    asset_class = models.CharField(max_length=10, choices=AssetClass.choices, default=AssetClass.EQUITY)
    management = models.CharField(max_length=10, choices=Management.choices, default=Management.SELF)
    advisor = models.CharField(max_length=80, blank=True, help_text="e.g. Weekend Investing, ET Money")
    benchmark = models.CharField(max_length=80, blank=True, help_text="e.g. Nifty 50 TRI")
    needs_review = models.BooleanField(default=True, help_text="New holding: confirm its tags.")
    isin = models.CharField(max_length=12, blank=True, db_index=True)
    amfi_code = models.CharField(max_length=10, blank=True, db_index=True)
    is_passive = models.BooleanField(default=False, help_text="Index fund / ETF")
    last_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    last_price_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.name or self.identifier


class NavPrice(models.Model):
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="navs")
    date = models.DateField()
    nav = models.DecimalField(max_digits=14, decimal_places=4)

    class Meta:
        unique_together = [("instrument", "date")]
        ordering = ["-date"]


class HoldingsPull(models.Model):
    """Holding-level picture of one account at one moment (a Kite fetch or a CAS statement)."""

    class Source(models.TextChoices):
        KITE = "kite", "Zerodha Kite"
        CAS = "cas", "CAS statement"

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="pulls")
    source = models.CharField(max_length=10, choices=Source.choices)
    as_of = models.DateField()
    fetched_at = models.DateTimeField(auto_now_add=True)
    total_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-as_of", "-fetched_at"]

    def __str__(self):
        return f"{self.account} {self.get_source_display()} {self.as_of}"

    def current_value(self):
        """Revalue at the latest known price (AMFI NAV for funds); falls back to the pull value."""
        total = Decimal("0")
        for pos in self.positions.select_related("instrument"):
            inst = pos.instrument
            if inst.last_price and inst.last_price_date and inst.last_price_date > self.as_of:
                total += (pos.quantity * inst.last_price).quantize(Decimal("0.01"))
            else:
                total += pos.value
        return total


class KiteSession(models.Model):
    """Daily Kite Connect access token (Kite expires them around 6am the next day)."""

    person = models.OneToOneField(Person, on_delete=models.CASCADE, related_name="kite_session")
    access_token = models.CharField(max_length=100)
    kite_user_id = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField()


class Position(models.Model):
    pull = models.ForeignKey("HoldingsPull", on_delete=models.CASCADE, related_name="positions")
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="positions")
    quantity = models.DecimalField(max_digits=16, decimal_places=4)
    avg_cost = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    price = models.DecimalField(max_digits=14, decimal_places=4)
    value = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        unique_together = [("pull", "instrument")]


class MonthlyExpense(models.Model):
    month = models.DateField(unique=True, help_text="First day of the month")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    includes_emi = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["month"]


class Household(models.Model):
    """Singleton with FI settings and the inputs for YMOYL calculations."""

    monthly_net_income = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    hours_worked_per_month = models.DecimalField(max_digits=6, decimal_places=1, default=200)
    safe_withdrawal_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.0333"))
    inflation = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.06"))
    emergency_fund_months = models.PositiveIntegerField(default=12)
    target_fi_date = models.DateField(null=True, blank=True)
    work_costs_monthly = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="YMOYL: money spent only because you work (commute, work clothes, meals out, childcare).",
    )
    planned_monthly_investment = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Used for projections until there are 3 tracked months of actual investing.",
    )
    expected_returns = models.JSONField(
        default=dict, blank=True,
        help_text='Annual nominal return per asset class, e.g. {"equity": 0.11}. Blank = conservative defaults.',
    )

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class AllocationTarget(models.Model):
    asset_class = models.CharField(max_length=10, choices=AssetClass.choices, unique=True)
    target_pct = models.DecimalField(max_digits=5, decimal_places=2)
    band_pct = models.DecimalField(max_digits=5, decimal_places=2, default=5)


class Review(models.Model):
    class Cadence(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        ADHOC = "adhoc", "Ad hoc"

    created_at = models.DateTimeField(auto_now_add=True)
    cadence = models.CharField(max_length=10, choices=Cadence.choices)
    model = models.CharField(max_length=60)
    input_json = models.JSONField()
    output_markdown = models.TextField(blank=True)
    output_json = models.JSONField(null=True, blank=True)
    question = models.TextField(blank=True, help_text="For ad-hoc 'Ask' reviews")
    status = models.CharField(max_length=10, default="done")  # running / done / failed
    error = models.TextField(blank=True)
    usage = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Recommendation(models.Model):
    class Verdict(models.TextChoices):
        EXIT = "exit", "Exit"
        REDUCE = "reduce", "Reduce"
        HOLD = "hold", "Hold"
        ADD = "add", "Add more"
        ACTION = "action", "Action"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        DONE = "done", "Done"
        DISMISSED = "dismissed", "Dismissed"

    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="recommendations")
    instrument = models.ForeignKey(Instrument, null=True, blank=True, on_delete=models.SET_NULL)
    account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.SET_NULL)
    verdict = models.CharField(max_length=10, choices=Verdict.choices, default=Verdict.ACTION)
    title = models.CharField(max_length=200)
    rationale = models.TextField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    urgency = models.CharField(max_length=10, default="normal")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    status_note = models.CharField(max_length=200, blank=True)
