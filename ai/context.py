"""Builds the compact JSON snapshot of the family's finances that each review sends to Claude.

Everything Claude reasons about comes from here; if it isn't in this payload,
Claude is told to list it as a data gap rather than guess.
"""
import datetime as dt
import logging
from collections import defaultdict

from django.db.models import Min

from core import analytics
from core.models import Account, CashFlow, HoldingsPull, Household, Instrument, Recommendation
from fi import data as fi_data
from fi import engine
from integrations import mfapi

log = logging.getLogger(__name__)


def _r(x, nd=0):
    return round(float(x), nd) if x is not None else None


def _fy(date):
    start = date.year if date.month >= 4 else date.year - 1
    return f"FY{start}-{str(start + 1)[-2:]}"


def build(cadence, today=None, fetch_history=True):
    today = today or dt.date.today()
    periods = analytics.periods()
    if not periods:
        raise ValueError("No snapshots yet - add a month-end entry first.")
    latest = periods[-1]
    snap = latest.snapshot
    hh = Household.get()
    total_assets = float(latest.investments.closing + latest.cash.closing)

    ctx = {
        "as_of": str(snap.date),
        "today": str(today),
        "cadence": cadence,
        "household": {
            "people": sorted(latest.by_person),
            "monthly_net_income": _r(hh.monthly_net_income) or None,
            "hours_worked_per_month": _r(hh.hours_worked_per_month),
            "work_costs_monthly": _r(hh.work_costs_monthly),
            "safe_withdrawal_rate": float(hh.safe_withdrawal_rate),
            "inflation": float(hh.inflation),
            "emergency_fund_months": hh.emergency_fund_months,
            "target_fi_date": str(hh.target_fi_date) if hh.target_fi_date else None,
            "family_notes": hh.categorization_notes or None,
        },
        "net_worth": _net_worth(periods),
        "accounts": _accounts(snap, total_assets),
        "fi": _fi(periods, hh),
        "holdings": _holdings(snap, total_assets, fetch_history),
        "benchmarks": {"Nifty 50": mfapi.benchmark_returns("Nifty 50")} if fetch_history else {},
        "tax_context": {
            "financial_year": _fy(today),
            "note": "Realised capital gains already booked this FY are NOT tracked by the app; ask before assuming the exemption is unused.",
        },
        "data_freshness": _freshness(snap, periods),
        "previous_recommendations": _previous_recs(),
    }
    return ctx


def _net_worth(periods):
    latest = periods[-1]
    recent = []
    for p in periods[-7:]:
        recent.append({
            "date": str(p.snapshot.date),
            "net_worth": _r(p.net_worth),
            "change": _r(p.change),
            "money_invested": _r(p.investments.invested) if p.market_gain is not None else None,
            "market_gain": _r(p.market_gain),
            "market_gain_pct": _r(p.market_gain_pct, 4),
            "bank_cash": _r(p.cash.closing),
            "loans": _r(p.loans.closing),
            "flows_tracked": p.flows_tracked,
        })
    return {
        "total": _r(latest.net_worth),
        "by_person": {k: _r(v) for k, v in latest.by_person.items()},
        "recent_periods": recent,
        "investments_xirr_tracked": _r(analytics.xirr_for(Account.investment_accounts()), 4),
    }


def _accounts(snap, total_assets):
    out = []
    for b in snap.balances.select_related("account__owner").order_by("account__owner_id", "account__sort_order"):
        a = b.account
        out.append({
            "owner": a.owner.name, "name": a.name, "kind": a.kind, "asset_class": a.asset_class,
            "value": _r(b.value),
            "share_of_assets": None if a.is_liability else _r(float(b.value) / total_assets, 4) if total_assets else None,
            **({"emi": _r(a.emi), "interest_rate": float(a.interest_rate) if a.interest_rate else None,
                "loan_end_date": str(a.loan_end_date) if a.loan_end_date else None} if a.is_liability else {}),
        })
    return out


def _fi(periods, hh):
    built = fi_data.build_inputs(periods=periods)
    if not built:
        return {"available": False, "reason": "Monthly expenses not entered on the Plan page."}
    inp, source = built
    proj = engine.project(inp)
    mc = engine.monte_carlo(inp, runs=1000)
    prob_target = None
    if hh.target_fi_date:
        prob_target = next((p["prob"] for p in mc["prob_by_year"] if p["year"] >= hh.target_fi_date.year), None)
    return {
        "available": True,
        "monthly_expenses": _r(inp.monthly_expenses),
        "expenses_include_emi": inp.expenses_include_emi,
        "core_monthly_expenses_ex_emi": _r(inp.core_expenses),
        "emergency_fund_required": _r(inp.emergency_fund),
        "bank_cash": _r(inp.bank_cash),
        "cash_above_emergency_fund": _r(inp.excess_cash),
        "corpus": _r(inp.corpus),
        "fi_target": _r(inp.fi_target),
        "progress": _r(inp.progress, 4),
        "safe_monthly_income": _r(inp.safe_monthly_income),
        "loan_outstanding": _r(inp.loan_outstanding),
        "monthly_investment_used": _r(inp.monthly_investment),
        "monthly_investment_source": source,
        "assumed_blended_return": _r(inp.blended_return, 4),
        "projected_crossover": str(proj["crossover_date"]) if proj["crossover_date"] else None,
        "monte_carlo_median_crossover_year": mc["median_crossover_year"],
        "monte_carlo_prob_fi_by_target_date": prob_target,
        "allocation_vs_target": [
            {"asset_class": d["asset_class"], "value": _r(d["value"]), "actual": _r(d["actual"], 4),
             "target": _r(d["target"], 4), "out_of_band": d["out_of_band"],
             "rebalance_amount": _r(d["rebalance_amount"])}
            for d in fi_data.drift(inp)
        ],
        "savings_rates": [{"date": str(s["date"]), "rate": _r(s["rate"], 3)}
                          for s in fi_data.savings_rates(periods, float(hh.monthly_net_income))][-6:],
    }


def _plan_type(name):
    n = name.upper()
    if "DIRECT" in n:
        return "direct"
    if "REGULAR" in n:
        return "regular"
    return None


def _holdings(snap, total_assets, fetch_history):
    """Latest holding-level view per investment account in the snapshot (or the latest pull)."""
    self_managed, advisor = [], defaultdict(lambda: {"value": 0.0, "cost": 0.0, "count": 0, "top": []})
    untagged = 0
    first_buy = dict(CashFlow.objects.filter(instrument__isnull=False, amount__gt=0)
                     .values("instrument_id").annotate(d=Min("date")).values_list("instrument_id", "d"))
    accounts_with_pulls = 0
    for acc in Account.investment_accounts().filter(is_active=True).select_related("owner"):
        bal = snap.balances.filter(account=acc).first()
        pull = (bal.pull if bal and bal.pull else None) or acc.pulls.first()
        if not pull:
            continue
        accounts_with_pulls += 1
        for pos in pull.positions.select_related("instrument"):
            inst = pos.instrument
            if inst.identifier == "CASH-ZERODHA":
                continue
            untagged += inst.needs_review
            value = float(pos.value)
            cost = float(pos.avg_cost * pos.quantity) if pos.avg_cost else None
            if inst.management == Instrument.Management.ADVISOR:
                g = advisor[inst.advisor or "Unnamed advisor"]
                g["value"] += value
                g["cost"] += cost or value
                g["count"] += 1
                g["top"].append((value, inst.name or inst.identifier))
                continue
            row = {
                "instrument_id": inst.id, "name": inst.name or inst.identifier, "identifier": inst.identifier,
                "kind": inst.kind, "asset_class": inst.asset_class, "is_passive": inst.is_passive,
                "plan_type": _plan_type(inst.name or ""), "owner": acc.owner.name, "account": acc.name,
                "value": _r(value), "cost": _r(cost), "unrealised_gain": _r(value - cost) if cost else None,
                "unrealised_gain_pct": _r((value - cost) / cost, 4) if cost else None,
                "weight_of_assets": _r(value / total_assets, 4) if total_assets else None,
                "first_purchase": str(first_buy[inst.id]) if inst.id in first_buy else None,
                "data_as_of": str(pull.as_of), "source": pull.source,
            }
            if inst.kind == Instrument.Kind.MF and inst.amfi_code and fetch_history:
                try:
                    mfapi.store_history(inst)
                    row["trailing_returns"] = mfapi.trailing_returns(mfapi.history(inst.amfi_code))
                except Exception as e:  # network or data problems shouldn't stop the review
                    log.warning("NAV history for %s failed: %s", inst, e)
                    row["trailing_returns"] = None
            self_managed.append(row)
    self_managed.sort(key=lambda r: -(r["value"] or 0))
    groups = []
    for name, g in sorted(advisor.items(), key=lambda kv: -kv[1]["value"]):
        groups.append({
            "advisor": name, "value": _r(g["value"]), "holdings_count": g["count"],
            "weight_of_assets": _r(g["value"] / total_assets, 4) if total_assets else None,
            "unrealised_gain_pct": _r((g["value"] - g["cost"]) / g["cost"], 4) if g["cost"] else None,
            "top_holdings": [n for _, n in sorted(g["top"], reverse=True)[:5]],
        })
    return {
        "holding_level_data_available": accounts_with_pulls > 0,
        "self_managed": self_managed,
        "advisor_managed_groups": groups,
        "holdings_with_unconfirmed_tags": untagged,
        "broker_account_performance": _broker_performance(),
    }


def _broker_performance():
    """Tracked-period market gain % per Zerodha/MF account, as a proxy for advisor strategy returns."""
    out = []
    for p in analytics.periods()[-12:]:
        if p.market_gain is None:
            continue
        for acc_id, b in p.by_account.items():
            acc = Account.objects.get(pk=acc_id)
            if acc.kind in ("broker", "mf") and b.gain_pct is not None:
                out.append({"date": str(p.snapshot.date), "account": str(acc), "gain_pct": _r(b.gain_pct, 4)})
    return out


def _freshness(snap, periods):
    out = {
        "latest_snapshot": str(snap.date),
        "tracked_periods": sum(1 for p in periods if p.market_gain is not None),
        "last_kite_pull": {},
        "last_cas": {},
        "latest_nav_date": None,
    }
    for pull in HoldingsPull.objects.select_related("account__owner").order_by("account_id", "-as_of"):
        key = "last_kite_pull" if pull.source == "kite" else "last_cas"
        out[key].setdefault(str(pull.account), str(pull.as_of))
    nav = Instrument.objects.exclude(last_price_date=None).order_by("-last_price_date").first()
    out["latest_nav_date"] = str(nav.last_price_date) if nav else None
    out["fd_maturity_dates_tracked"] = False
    return out


def _previous_recs(limit=20):
    rows = []
    for r in Recommendation.objects.select_related("review", "instrument").order_by("-review__created_at")[:limit]:
        rows.append({
            "date": str(r.review.created_at.date()), "verdict": r.verdict, "title": r.title,
            "instrument": r.instrument.name if r.instrument else None, "amount": _r(r.amount),
            "status": r.status, "your_note": r.status_note or None,
        })
    return rows
