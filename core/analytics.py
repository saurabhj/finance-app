"""Net worth, invested-vs-market-gain attribution and allocation.

Attribution for an investment account between two snapshots:
    market_gain = closing - opening - net_cash_flows
where net_cash_flows is money put in (+) or taken out (-) in (prev.date, cur.date].
Bank accounts are cash, not investments: their change is savings/spending plus
transfers, so they get no "gain" figure. Loans are subtracted from net worth.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Sum

from .models import Account, AssetClass, Balance, CashFlow, Snapshot

ZERO = Decimal("0")


@dataclass
class Bucket:
    opening: Decimal = ZERO
    invested: Decimal = ZERO
    closing: Decimal = ZERO

    @property
    def gain(self):
        return self.closing - self.opening - self.invested

    @property
    def gain_pct(self):
        base = self.opening + self.invested
        return (self.gain / base) if base else None


@dataclass
class Period:
    snapshot: Snapshot
    prev: Snapshot | None
    flows_tracked: bool
    investments: Bucket = field(default_factory=Bucket)
    cash: Bucket = field(default_factory=Bucket)
    loans: Bucket = field(default_factory=Bucket)
    by_person: dict = field(default_factory=dict)   # person name -> net worth
    by_account: dict = field(default_factory=dict)  # account id -> Bucket

    @property
    def net_worth(self):
        return self.investments.closing + self.cash.closing - self.loans.closing

    @property
    def prev_net_worth(self):
        return self.investments.opening + self.cash.opening - self.loans.opening

    @property
    def change(self):
        return self.net_worth - self.prev_net_worth if self.prev else None

    @property
    def market_gain(self):
        """Market gain on investments; None when money invested wasn't recorded."""
        if not self.prev or not self.flows_tracked:
            return None
        return self.investments.gain

    @property
    def market_gain_pct(self):
        if self.market_gain is None:
            return None
        return self.investments.gain_pct

    @property
    def savings(self):
        """Net worth change not explained by market gain = money saved this period."""
        if self.market_gain is None:
            return None
        return self.change - self.market_gain


def _balances_by_snapshot():
    data = defaultdict(dict)
    for snap_id, acc_id, value in Balance.objects.values_list("snapshot_id", "account_id", "value"):
        data[snap_id][acc_id] = value
    return data


def _flows(account_ids, start, end):
    """Net cash flow per account for dates in (start, end]."""
    qs = CashFlow.objects.filter(account_id__in=account_ids, date__lte=end)
    if start:
        qs = qs.filter(date__gt=start)
    return {row["account_id"]: row["total"] for row in qs.values("account_id").annotate(total=Sum("amount"))}


def periods():
    """One Period per snapshot, oldest first."""
    accounts = {a.id: a for a in Account.objects.select_related("owner")}
    balances = _balances_by_snapshot()
    result = []
    prev = None
    for snap in Snapshot.objects.order_by("date"):
        cur_vals = balances.get(snap.id, {})
        prev_vals = balances.get(prev.id, {}) if prev else {}
        flows = _flows(accounts.keys(), prev.date if prev else None, snap.date) if prev else {}
        p = Period(snapshot=snap, prev=prev, flows_tracked=snap.flows_tracked)
        person_nw = defaultdict(lambda: ZERO)
        for acc_id in set(cur_vals) | set(prev_vals):
            acc = accounts[acc_id]
            b = Bucket(
                opening=prev_vals.get(acc_id, ZERO),
                invested=flows.get(acc_id, ZERO) if acc.is_investment else ZERO,
                closing=cur_vals.get(acc_id, ZERO),
            )
            p.by_account[acc_id] = b
            target = p.loans if acc.is_liability else (p.investments if acc.is_investment else p.cash)
            target.opening += b.opening
            target.invested += b.invested
            target.closing += b.closing
            person_nw[acc.owner.name] += -b.closing if acc.is_liability else b.closing
        p.by_person = dict(person_nw)
        result.append(p)
        prev = snap
    return result


def allocation(snapshot):
    """Asset-class split of a snapshot's assets (loans excluded).

    Uses holding-level positions where an account has them, else the account's
    default asset class.
    """
    totals = defaultdict(lambda: ZERO)
    for bal in snapshot.balances.select_related("account", "pull"):
        acc = bal.account
        if acc.is_liability:
            continue
        positions = list(bal.pull.positions.select_related("instrument")) if bal.pull else []
        pos_total = sum((p.value for p in positions), ZERO)
        if not positions or not pos_total:
            totals[acc.asset_class] += bal.value
            continue
        # Split the entered balance in the proportions of the pull's holdings,
        # so the allocation always adds up to the balance you saved.
        for p in positions:
            totals[p.instrument.asset_class] += bal.value * p.value / pos_total
    grand = sum(totals.values()) or Decimal(1)
    labels = dict(AssetClass.choices)
    return [
        {"asset_class": k, "label": labels.get(k, k), "value": v, "pct": v / grand}
        for k, v in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


def xirr_for(accounts, min_days=30):
    """XIRR over the stretch where money invested is tracked.

    Starts at the snapshot just before the first tracked one (its value is treated
    as the initial investment) and ends at the latest snapshot. None until there
    are at least `min_days` of tracked history.
    """
    from .xirr import xirr

    ids = [a.id for a in accounts]
    snaps = list(Snapshot.objects.order_by("date"))
    first_tracked = next((i for i, s in enumerate(snaps) if s.flows_tracked), None)
    if first_tracked is None or first_tracked == 0:
        return None
    start, end = snaps[first_tracked - 1], snaps[-1]
    if (end.date - start.date).days < min_days:
        return None

    def value(snap):
        return Balance.objects.filter(snapshot=snap, account_id__in=ids).aggregate(t=Sum("value"))["t"] or ZERO

    flows = [(start.date, -value(start))]
    flows += [(d, -a) for d, a in CashFlow.objects.filter(
        account_id__in=ids, date__gt=start.date, date__lte=end.date).values_list("date", "amount")]
    flows.append((end.date, value(end)))
    return xirr(flows)
