"""Financial independence maths, following *Your Money or Your Life* (YMOYL).

Definitions used throughout:
- core expenses: monthly spending excluding loan EMIs (loans are treated as
  something to clear, not a forever expense).
- FI target: core expenses × 12 ÷ SWR  +  loans outstanding.
- corpus: investments + bank cash above the emergency fund.
- safe monthly income: (corpus − loan outstanding) × SWR ÷ 12.
- crossover (YMOYL's wall chart): the month safe income ≥ core expenses.
"""
import datetime as dt
import math
import random
from dataclasses import dataclass, field

DEFAULT_RETURNS = {"equity": 0.11, "gold": 0.08, "debt": 0.07, "cash": 0.035, "hybrid": 0.09}
VOLATILITY = {"equity": 0.18, "gold": 0.15, "debt": 0.03, "cash": 0.01, "hybrid": 0.10}
CORRELATION = 0.3
HORIZON_YEARS = 40


@dataclass
class FiInputs:
    as_of: dt.date
    investments: dict              # asset class -> value (investment accounts only)
    bank_cash: float
    monthly_expenses: float
    expenses_include_emi: bool = True
    loan_outstanding: float = 0.0
    emi: float = 0.0
    loan_rate: float = 0.0         # annual
    monthly_investment: float = 0.0
    swr: float = 0.0333
    inflation: float = 0.06
    emergency_months: int = 12
    returns: dict = field(default_factory=dict)
    loans: list = field(default_factory=list)  # [{"outstanding", "emi", "rate"}]; else the single-loan fields

    @property
    def loan_list(self):
        if self.loans:
            return self.loans
        if self.loan_outstanding:
            return [{"outstanding": self.loan_outstanding, "emi": self.emi, "rate": self.loan_rate}]
        return []

    # ---- derived
    @property
    def core_expenses(self):
        return max(self.monthly_expenses - (self.emi if self.expenses_include_emi else 0), 0.0)

    @property
    def emergency_fund(self):
        return self.emergency_months * self.monthly_expenses

    @property
    def excess_cash(self):
        return max(self.bank_cash - self.emergency_fund, 0.0)

    @property
    def corpus(self):
        return sum(self.investments.values()) + self.excess_cash

    @property
    def fi_target(self):
        return self.core_expenses * 12 / self.swr + self.loan_outstanding

    @property
    def progress(self):
        return self.corpus / self.fi_target if self.fi_target else None

    @property
    def safe_monthly_income(self):
        return max(self.corpus - self.loan_outstanding, 0.0) * self.swr / 12

    @property
    def weights(self):
        mix = dict(self.investments)
        mix["cash"] = mix.get("cash", 0.0) + self.excess_cash
        total = sum(mix.values()) or 1.0
        return {k: v / total for k, v in mix.items() if v > 0}

    def rate(self, asset_class):
        return float(self.returns.get(asset_class, DEFAULT_RETURNS.get(asset_class, 0.07)))

    @property
    def blended_return(self):
        return sum(w * self.rate(k) for k, w in self.weights.items())

    @property
    def blended_volatility(self):
        w = self.weights
        keys = list(w)
        var = 0.0
        for i in keys:
            for j in keys:
                rho = 1.0 if i == j else CORRELATION
                var += w[i] * w[j] * VOLATILITY.get(i, 0.1) * VOLATILITY.get(j, 0.1) * rho
        return math.sqrt(var)


def _add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


def project(inp: FiInputs, annual_return=None, years=HORIZON_YEARS):
    """Deterministic monthly projection. Returns dict with yearly series and crossover."""
    r = annual_return if annual_return is not None else inp.blended_return
    r_m = (1 + r) ** (1 / 12) - 1
    i_m = (1 + inp.inflation) ** (1 / 12) - 1
    loans = [dict(l) for l in inp.loan_list]
    corpus, loan = inp.corpus, sum(l["outstanding"] for l in loans)
    expenses, contrib = inp.core_expenses, inp.monthly_investment
    crossover = None
    series = []
    for t in range(years * 12 + 1):
        income = max(corpus - loan, 0) * inp.swr / 12
        if crossover is None and income >= expenses:
            crossover = t
        if t % 12 == 0:
            series.append({"year": inp.as_of.year + t // 12, "corpus": corpus, "loan": loan,
                           "safe_income": income, "expenses": expenses})
        # step one month: pay each loan; once a loan is gone its EMI becomes investable
        freed_emi = 0.0
        for l in loans:
            if l["outstanding"] > 0:
                if l["emi"]:
                    l["outstanding"] = max(l["outstanding"] * (1 + l["rate"] / 12) - l["emi"], 0.0)
            else:
                freed_emi += l["emi"]
        loan = sum(l["outstanding"] for l in loans)
        corpus = corpus * (1 + r_m) + contrib + freed_emi
        expenses *= 1 + i_m
        contrib *= 1 + i_m
    return {
        "series": series,
        "crossover_months": crossover,
        "crossover_date": _add_months(inp.as_of, crossover) if crossover is not None else None,
        "annual_return": r,
    }


def monte_carlo(inp: FiInputs, runs=2000, years=HORIZON_YEARS, seed=42):
    """Yearly simulation with normally distributed returns. Returns percentile bands
    of corpus and the probability of having crossed over by each year."""
    rng = random.Random(seed)
    mu, sigma = inp.blended_return, inp.blended_volatility
    paths, crossed_at = [], []
    for _ in range(runs):
        loans = [dict(l) for l in inp.loan_list]
        corpus, expenses, contrib = inp.corpus, inp.core_expenses, inp.monthly_investment
        loan = sum(l["outstanding"] for l in loans)
        path, hit = [corpus], 0 if corpus - loan >= expenses * 12 / inp.swr else None
        for y in range(1, years + 1):
            ret = rng.gauss(mu, sigma)
            extra = sum(l["emi"] * 12 for l in loans if l["outstanding"] <= 0)
            for l in loans:
                if l["outstanding"] > 0 and l["emi"]:
                    for _m in range(12):
                        l["outstanding"] = max(l["outstanding"] * (1 + l["rate"] / 12) - l["emi"], 0.0)
            loan = sum(l["outstanding"] for l in loans)
            corpus = max(corpus * (1 + ret) + contrib * 12 + extra, 0.0)
            expenses *= 1 + inp.inflation
            contrib *= 1 + inp.inflation
            path.append(corpus)
            if hit is None and (corpus - loan) * inp.swr >= expenses * 12:
                hit = y
        paths.append(path)
        crossed_at.append(hit)

    def pct(values, q):
        s = sorted(values)
        return s[min(int(q * len(s)), len(s) - 1)]

    bands = []
    for y in range(years + 1):
        col = [p[y] for p in paths]
        bands.append({"year": inp.as_of.year + y, "p10": pct(col, 0.10), "p50": pct(col, 0.50), "p90": pct(col, 0.90)})
    prob_by_year = [
        {"year": inp.as_of.year + y, "prob": sum(1 for h in crossed_at if h is not None and h <= y) / runs}
        for y in range(years + 1)
    ]
    hits = sorted(h for h in crossed_at if h is not None)
    median = hits[len(hits) // 2] if len(hits) >= runs / 2 else None
    return {"bands": bands, "prob_by_year": prob_by_year,
            "median_crossover_year": inp.as_of.year + median if median is not None else None,
            "mu": mu, "sigma": sigma}


def real_hourly_wage(monthly_income, work_costs, hours_per_month):
    """YMOYL step 2: what an hour of your life energy is actually worth."""
    if not hours_per_month:
        return None
    return (monthly_income - work_costs) / hours_per_month


def life_energy_hours(amount, wage):
    return amount / wage if wage else None
