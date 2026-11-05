from decimal import Decimal

from django.db import models


class AssetClass(models.TextChoices):
    EQUITY = "equity", "Equity"
    GOLD = "gold", "Gold"
    DEBT = "debt", "Debt"
    CASH = "cash", "Cash"
    HYBRID = "hybrid", "Hybrid"
    LIABILITY = "liability", "Liability"


class PersonQuerySet(models.QuerySet):
    def people(self):
        """Real people (excludes the 'Joint' owner used for jointly held accounts and loans)."""
        return self.filter(is_joint=False)


class Person(models.Model):
    name = models.CharField(max_length=50, unique=True)
    user = models.OneToOneField(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL
    )
    is_joint = models.BooleanField(default=False, help_text="Owner for things held jointly by both of you.")

    objects = PersonQuerySet.as_manager()

    class Meta:
        ordering = ["is_joint", "id"]

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
        CARD = "card", "Credit card"

    NON_INVESTMENT_KINDS = ("bank", "loan", "card")

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
    match_hints = models.CharField(
        max_length=200, blank=True,
        help_text="Account / card numbers that appear on its statements, comma-separated (full number or last 4-5 digits). Used to sort bulk uploads.",
    )
    statements_from = models.DateField(
        null=True, blank=True,
        help_text="First month this account needs statements for (e.g. when it was opened). Blank = the checklist's default.",
    )
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
        return self.kind not in self.NON_INVESTMENT_KINDS

    @property
    def is_liability(self):
        return self.kind in (self.Kind.LOAN, self.Kind.CARD)

    @classmethod
    def investment_accounts(cls):
        return cls.objects.exclude(kind__in=cls.NON_INVESTMENT_KINDS)


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
        BANK = "bank", "Bank statement"

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
    categorization_notes = models.TextField(
        blank=True,
        help_text="Plain-English instructions Claude follows when reading and categorising statements.",
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


# ------------------------------------------------------------------ statements

class Category(models.TextChoices):
    INVESTMENT = "investment", "Investment (money in)"
    INVESTMENT_RETURN = "inv_return", "Investment payout (money out)"
    TRANSFER = "transfer", "Transfer between our accounts"
    CARD_PAYMENT = "card_payment", "Credit card bill payment"
    LOAN_EMI = "loan_emi", "Loan EMI"
    INCOME = "income", "Income (salary, interest…)"
    EXPENSE = "expense", "Spending"
    HOME = "home", "Home purchase & renovation"
    REFUND = "refund", "Refund / cashback"
    REIMBURSABLE = "reimbursable", "Paid for work (reimbursable)"
    REIMBURSEMENT = "reimbursement", "Reimbursement received"
    IGNORE = "ignore", "Ignore"


class SpendCategory(models.TextChoices):
    GROCERIES = "groceries", "Groceries"
    DINING = "dining", "Dining & food delivery"
    SHOPPING = "shopping", "Shopping"
    TRAVEL = "travel", "Travel"
    TRANSPORT = "transport", "Fuel & transport"
    UTILITIES = "utilities", "Utilities & bills"
    HOUSEHOLD = "household", "Rent, help & household"
    HEALTH = "health", "Health"
    EDUCATION = "education", "Education"
    ENTERTAINMENT = "entertainment", "Entertainment"
    SUBSCRIPTIONS = "subscriptions", "Subscriptions"
    INSURANCE = "insurance", "Insurance"
    TAXES = "taxes", "Taxes"
    GIFTS = "gifts", "Gifts & donations"
    OTHER = "other", "Other"


class StatementUpload(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="statements")
    filename = models.CharField(max_length=200)
    sha256 = models.CharField(max_length=64)
    parser = models.CharField(max_length=20)  # table / claude
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    added = models.PositiveIntegerField(default=0)
    duplicates = models.PositiveIntegerField(default=0)
    warning = models.TextField(blank=True)

    class Meta:
        ordering = ["-uploaded_at"]


class CategoryRule(models.Model):
    """Description keyword -> category. User rules win over built-in ones."""

    class Target(models.TextChoices):
        NONE = "", "—"
        ZERODHA = "zerodha", "Zerodha account of the statement owner"
        MF = "mf", "Mutual fund account of the statement owner"
        FD = "fd", "FD account of the statement owner"

    keyword = models.CharField(max_length=100, help_text="Case-insensitive text found in the description")
    applies_to = models.CharField(max_length=10, choices=[("any", "Any"), ("bank", "Bank"), ("card", "Card")], default="any")
    direction = models.CharField(max_length=10, choices=[("any", "Any"), ("debit", "Money out"), ("credit", "Money in")], default="any")
    category = models.CharField(max_length=20, choices=Category.choices)
    spend_category = models.CharField(max_length=20, choices=SpendCategory.choices, blank=True)
    target = models.CharField(max_length=10, choices=Target.choices, blank=True)
    target_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    party = models.CharField(max_length=60, blank=True, help_text="Who reimburses, for reimbursement rules (e.g. Flourish)")
    is_builtin = models.BooleanField(default=False)
    hits = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["is_builtin", "-id"]

    def __str__(self):
        return f"{self.keyword} → {self.get_category_display()}"


class Transaction(models.Model):
    class Source(models.TextChoices):
        RULE = "rule", "Rule"
        AI = "ai", "Claude guess"
        MATCH = "match", "Matched"
        USER = "user", "You"

    statement = models.ForeignKey(StatementUpload, on_delete=models.CASCADE, related_name="transactions")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="transactions")
    date = models.DateField()
    description = models.CharField(max_length=300)
    amount = models.DecimalField(max_digits=14, decimal_places=2, help_text="+ money in, − money out")
    balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    category = models.CharField(max_length=20, choices=Category.choices, blank=True)
    spend_category = models.CharField(max_length=20, choices=SpendCategory.choices, blank=True)
    investment_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    party = models.CharField(max_length=60, blank=True, help_text="Who reimburses (for reimbursable / reimbursement rows)")
    source = models.CharField(max_length=10, choices=Source.choices, blank=True)
    rule = models.ForeignKey(CategoryRule, null=True, blank=True, on_delete=models.SET_NULL)
    confirmed = models.BooleanField(default=False)
    cash_flow = models.ForeignKey(CashFlow, null=True, blank=True, on_delete=models.SET_NULL, related_name="bank_transactions")
    matched_with = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                     help_text="The other side of a transfer between our accounts")
    dedupe_key = models.CharField(max_length=64, unique=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-date", "id"]

    def __str__(self):
        return f"{self.date} {self.description[:40]} {self.amount}"


# ------------------------------------------------------------------ loans

class Loan(models.Model):
    """Loan details behind a 'loan' Account. The Account's monthly Balance stays the official outstanding."""

    class Type(models.TextChoices):
        HOME = "home", "Home loan"
        CAR = "car", "Car / auto loan"
        PERSONAL = "personal", "Personal loan"
        EDUCATION = "education", "Education loan"
        OTHER = "other", "Other"

    class RateType(models.TextChoices):
        FLOATING = "floating", "Floating"
        FIXED = "fixed", "Fixed"

    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name="loan")
    lender = models.CharField(max_length=80, blank=True)
    loan_type = models.CharField(max_length=10, choices=Type.choices, default=Type.HOME)
    rate_type = models.CharField(max_length=10, choices=RateType.choices, default=RateType.FLOATING)
    principal = models.DecimalField(max_digits=14, decimal_places=2,
                                    help_text="Amount borrowed (or today's outstanding if you start tracking mid-way)")
    start_date = models.DateField(help_text="Disbursement date (or the date of the outstanding above)")
    first_emi_date = models.DateField(null=True, blank=True, help_text="Defaults to one month after the start date")
    tenure_months = models.PositiveIntegerField(help_text="Original tenure (or months remaining if starting mid-way)")
    emi = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                              help_text="Starting EMI as per the bank; left blank it's calculated")
    statement_keyword = models.CharField(max_length=80, blank=True,
                                         help_text="Text that identifies this loan's EMIs in bank statements, e.g. 'Auto Loan XX78951'")
    notes = models.TextField(blank=True)
    closed = models.BooleanField(default=False)

    def __str__(self):
        return str(self.account)


class LoanRate(models.Model):
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="rates")
    effective_date = models.DateField()
    rate = models.DecimalField(max_digits=6, decimal_places=4, help_text="Annual, e.g. 0.0760")
    new_emi = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                  help_text="If the bank changed the EMI with this rate change. Blank = EMI unchanged, tenure adjusts.")
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["effective_date", "id"]


class LoanPrepayment(models.Model):
    class Effect(models.TextChoices):
        TENURE = "tenure", "Reduce tenure (EMI unchanged)"
        EMI = "emi", "Reduce EMI (tenure unchanged)"

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="prepayments")
    date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    effect = models.CharField(max_length=10, choices=Effect.choices, default=Effect.TENURE)
    transaction = models.ForeignKey("Transaction", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["date", "id"]


class LoanCheckpoint(models.Model):
    """Outstanding as reported by the bank; the schedule re-anchors to it."""

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="checkpoints")
    date = models.DateField()
    outstanding = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["date", "id"]


class UploadBatch(models.Model):
    """A bulk statement upload processed in the background."""

    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=12, default="queued")  # queued / running / done
    passwords = models.TextField(blank=True, help_text="One per line; wiped when the batch finishes")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def counts(self):
        from collections import Counter
        return Counter(self.items.values_list("status", flat=True))


class UploadItem(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        NEEDS_ACCOUNT = "needs_account", "Needs account"
        PROCESSING = "processing", "Reading…"
        DONE = "done", "Imported"
        DUPLICATE = "duplicate", "Already uploaded"
        FAILED = "failed", "Failed"

    batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name="items")
    filename = models.CharField(max_length=255)
    path = models.CharField(max_length=500, help_text="Temporary copy under data/uploads (deleted after import)")
    sha256 = models.CharField(max_length=64)
    account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.SET_NULL)
    detected_by = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.QUEUED)
    message = models.TextField(blank=True)
    statement = models.ForeignKey(StatementUpload, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["id"]
