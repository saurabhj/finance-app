"""Loan amortisation with rate changes, prepayments and bank-reported checkpoints.

Month by month from the first EMI:
  interest  = opening balance × rate in force that month ÷ 12
  principal = EMI − interest
then prepayments in that month reduce the balance (cutting tenure, or the EMI if
chosen), and a bank-reported outstanding re-anchors the balance so small
differences in the bank's daily-interest method don't build up.
Rate change without a new EMI: EMI stays, tenure stretches or shrinks (how Indian
banks treat floating loans by default).
"""
import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")
MAX_MONTHS = 600


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(y, m, min(d.day, last))


def annuity(balance, annual_rate, months):
    """Standard EMI: P·r·(1+r)^n / ((1+r)^n − 1)."""
    balance, months = Decimal(balance), int(months)
    if months <= 0:
        return balance
    r = Decimal(annual_rate) / 12
    if r == 0:
        return (balance / months).quantize(CENT, ROUND_HALF_UP)
    f = (1 + r) ** months
    return (balance * r * f / (f - 1)).quantize(CENT, ROUND_HALF_UP)


@dataclass
class Row:
    n: int
    date: dt.date
    rate: Decimal
    opening: Decimal
    emi: Decimal
    interest: Decimal
    principal: Decimal
    prepayment: Decimal
    closing: Decimal
    past: bool
    note: str = ""


@dataclass
class Schedule:
    rows: list
    today: dt.date
    principal: Decimal
    warnings: list = field(default_factory=list)

    def _upto(self, date):
        return [r for r in self.rows if r.date <= date]

    def outstanding_on(self, date):
        done = self._upto(date)
        return done[-1].closing if done else self.principal

    @property
    def outstanding(self):
        return self.outstanding_on(self.today)

    @property
    def past(self):
        return [r for r in self.rows if r.past]

    @property
    def future(self):
        return [r for r in self.rows if not r.past]

    @property
    def interest_paid(self):
        return sum((r.interest for r in self.past), ZERO)

    @property
    def principal_paid(self):
        return self.principal - self.outstanding

    @property
    def interest_remaining(self):
        return sum((r.interest for r in self.future), ZERO)

    @property
    def prepaid(self):
        return sum((r.prepayment for r in self.past), ZERO)

    @property
    def payoff_date(self):
        return self.rows[-1].date if self.rows else None

    @property
    def months_left(self):
        return len(self.future)

    @property
    def current(self):
        """The next row due (or the last one if the loan is finished)."""
        fut = self.future
        return fut[0] if fut else (self.rows[-1] if self.rows else None)

    @property
    def pct_repaid(self):
        return (self.principal_paid / self.principal) if self.principal else ZERO

    def by_fy(self):
        """Totals per Indian financial year (Apr–Mar)."""
        out = {}
        for r in self.rows:
            fy = r.date.year if r.date.month >= 4 else r.date.year - 1
            t = out.setdefault(fy, {"fy": f"FY{fy}-{str(fy + 1)[-2:]}", "emi": ZERO, "interest": ZERO,
                                    "principal": ZERO, "prepayment": ZERO, "closing": ZERO, "past": True})
            t["emi"] += r.emi
            t["interest"] += r.interest
            t["principal"] += r.principal
            t["prepayment"] += r.prepayment
            t["closing"] = r.closing
            t["past"] = t["past"] and r.past
        return list(out.values())

    def fy_totals(self, date):
        fy = date.year if date.month >= 4 else date.year - 1
        return next((t for t in self.by_fy() if t["fy"].startswith(f"FY{fy}-")), None)


def build(principal, start_date, tenure_months, rates, emi=None, first_emi_date=None,
          prepayments=(), checkpoints=(), today=None):
    """rates: [(effective_date, annual_rate, new_emi|None)] sorted; prepayments: [(date, amount, effect)];
    checkpoints: [(date, outstanding)]."""
    today = today or dt.date.today()
    principal = Decimal(principal)
    rates = sorted(rates, key=lambda x: x[0])
    if not rates:
        raise ValueError("A loan needs at least one interest rate.")
    first = first_emi_date or add_months(start_date, 1)
    rate = Decimal(rates[0][1])
    cur_emi = Decimal(emi) if emi else annuity(principal, rate, tenure_months)
    end_n = tenure_months  # for 'reduce EMI' prepayments: keep the original end
    prepayments = sorted(prepayments, key=lambda x: x[0])
    checkpoints = sorted(checkpoints, key=lambda x: x[0])
    applied_rates = {0}
    bal = principal
    rows, warnings = [], []
    prev_date = start_date
    n = 0
    while bal > CENT and n < MAX_MONTHS:
        date = add_months(first, n)
        notes = []
        # rate changes effective on or before this EMI date
        for i, (eff, r, new_emi) in enumerate(rates):
            if i in applied_rates or eff > date:
                continue
            applied_rates.add(i)
            rate = Decimal(r)
            if new_emi:
                cur_emi = Decimal(new_emi)
            notes.append(f"rate {rate * 100:.2f}%" + (f", EMI {new_emi:,.0f}" if new_emi else ""))
        # a bank-reported balance dated before this EMI is the starting point for it
        for cdate, outstanding in checkpoints:
            if prev_date < cdate < date:
                diff = Decimal(outstanding) - bal
                if abs(diff) >= 1:
                    notes.append(f"bank balance {Decimal(outstanding):,.0f} ({diff:+,.0f})")
                bal = Decimal(outstanding)
        opening = bal
        interest = (bal * rate / 12).quantize(CENT, ROUND_HALF_UP)
        pay = min(cur_emi, bal + interest)
        if cur_emi <= interest and not warnings:
            warnings.append(f"From {date:%b %Y} the EMI doesn't cover the interest; the balance would grow.")
        principal_part = pay - interest
        if ZERO < bal - principal_part < Decimal("100"):  # rounding leftovers go into this EMI
            principal_part = bal
            pay = principal_part + interest
        bal -= principal_part
        prepaid = ZERO
        for pdate, amt, effect in prepayments:
            if prev_date < pdate <= date:
                amt = min(Decimal(amt), bal)
                bal -= amt
                prepaid += amt
                notes.append(f"prepaid {amt:,.0f}")
                if effect == "emi" and bal > CENT:
                    cur_emi = annuity(bal, rate, max(end_n - n - 1, 1))
        for cdate, outstanding in checkpoints:  # reported on the EMI date itself = after this EMI
            if cdate == date:
                diff = Decimal(outstanding) - bal
                if abs(diff) >= 1:
                    notes.append(f"bank balance {Decimal(outstanding):,.0f} ({diff:+,.0f})")
                bal = Decimal(outstanding)
        rows.append(Row(n=n + 1, date=date, rate=rate, opening=opening, emi=pay, interest=interest,
                        principal=principal_part, prepayment=prepaid, closing=max(bal, ZERO),
                        past=date <= today, note="; ".join(notes)))
        prev_date = date
        n += 1
    if n >= MAX_MONTHS and bal > CENT:
        warnings.append("Schedule runs past 50 years; check the EMI and rate.")
    return Schedule(rows=rows, today=today, principal=principal, warnings=warnings)


def for_loan(loan, today=None, include_prepayments=True):
    rates = [(r.effective_date, r.rate, r.new_emi) for r in loan.rates.all()]
    pre = [(p.date, p.amount, p.effect) for p in loan.prepayments.all()] if include_prepayments else []
    cps = [(c.date, c.outstanding) for c in loan.checkpoints.all()] if include_prepayments else []
    return build(loan.principal, loan.start_date, loan.tenure_months, rates, emi=loan.emi,
                 first_emi_date=loan.first_emi_date, prepayments=pre, checkpoints=cps, today=today)


def savings_from_prepayments(loan, today=None):
    """Interest and months saved vs. the same loan with no prepayments (and no checkpoints)."""
    if not loan.prepayments.exists():
        return None
    with_p = for_loan(loan, today)
    without = for_loan(loan, today, include_prepayments=False)
    return {
        "interest_saved": sum((r.interest for r in without.rows), ZERO) - sum((r.interest for r in with_p.rows), ZERO),
        "months_saved": len(without.rows) - len(with_p.rows),
        "payoff_without": without.payoff_date,
    }
