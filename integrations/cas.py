"""Import a CAMS/KFintech Consolidated Account Statement (CAS) PDF.

Get one from mfcentral.com or camsonline.com (Statements -> CAS, "Detailed",
with a password you choose). It covers every non-demat MF folio: the ET Money
index funds and the older active funds. Funds bought on Zerodha Coin live in
demat and come through the Kite pull instead.

What it creates:
- a HoldingsPull per person's MF account with one Position per scheme;
- CashFlows for purchases/SIPs (+), redemptions and dividend payouts (−) and
  switches, deduplicated so overlapping statements can be uploaded again.

A CAS can also list funds bought through Zerodha, which the Kite pull already
counts: only their money flows are kept, on the Zerodha account. Folios in
someone else's name (a child's, under the parent's PAN) are left out.
"""
import datetime as dt
import hashlib
from decimal import Decimal

from django.db import transaction

from core.models import Account, AssetClass, CashFlow, HoldingsPull, Instrument, Position

from .classify import get_or_create_instrument

INFLOW = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER"}
OUTFLOW = {"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER", "DIVIDEND_PAYOUT"}
SWITCHES = {"SWITCH_IN", "SWITCH_IN_MERGER", "SWITCH_OUT", "SWITCH_OUT_MERGER"}
ZERODHA_CODE = "INZ000031633"  # distributor code on every fund bought through Zerodha


def read_pdf(file, password):
    import casparser

    parsed = casparser.read_cas_pdf(file, password, output="dict")  # a typed model since casparser 1.0
    return parsed.model_dump() if hasattr(parsed, "model_dump") else parsed


def _first_name(name):
    return (name or "").upper().split()[0] if (name or "").strip() else ""


def _other_holder(folio, data):
    return _first_name(folio.get("name")) != _first_name((data.get("investor_info") or {}).get("name"))


def _value(scheme):
    return _d((scheme.get("valuation") or {}).get("value"))


def left_out(data):
    """What import_cas doesn't add as holdings: ({holder: value}, value already counted under Zerodha)."""
    holders, via_zerodha = {}, Decimal("0")
    for folio in data.get("folios", []):
        for s in folio.get("schemes", []):
            if _other_holder(folio, data):
                if _value(s):
                    holders[folio["name"]] = holders.get(folio["name"], Decimal("0")) + _value(s)
            elif s.get("advisor") == ZERODHA_CODE:
                via_zerodha += _value(s)
    return holders, via_zerodha


def mf_account(person):
    acc = Account.objects.filter(owner=person, kind=Account.Kind.MF, is_active=True).first()
    if acc is None:
        acc = Account.objects.create(owner=person, name="Mutual funds", kind=Account.Kind.MF,
                                     asset_class=AssetClass.EQUITY)
    return acc


def _date(v):
    if isinstance(v, dt.date):
        return v
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(str(v), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date {v!r}")


def _d(v):
    return Decimal(str(v)) if v not in (None, "") else Decimal("0")


def _type(t):
    return getattr(t, "name", None) or str(t).split(".")[-1]


@transaction.atomic
def import_cas(data, account):
    """`data` is casparser's dict output. Returns (pull, flows_created, flows_skipped)."""
    positions, as_of = [], None
    created = skipped = 0
    broker = Account.objects.filter(owner=account.owner, kind=Account.Kind.BROKER, is_active=True).first()
    for folio in data.get("folios", []):
        if _other_holder(folio, data):
            continue
        for s in folio.get("schemes", []):
            units = _d(s.get("close"))
            if not units and not s.get("transactions"):
                continue  # a closed fund with nothing in this period
            via_zerodha = s.get("advisor") == ZERODHA_CODE
            isin = s.get("isin") or ""
            inst = get_or_create_instrument(
                isin or f"{folio['folio']}:{s['scheme'][:20]}", s["scheme"], Instrument.Kind.MF,
                isin=isin, amfi_code=str(s.get("amfi") or ""), via_et_money=not via_zerodha,
            )
            val = s.get("valuation") or {}
            if units > 0 and val and not via_zerodha:
                vdate = _date(val["date"])
                as_of = max(as_of, vdate) if as_of else vdate
                cost = _d(val.get("cost"))
                positions.append((inst, units, (cost / units) if cost else None, _d(val["nav"]), _d(val["value"])))
                if not inst.last_price_date or vdate >= inst.last_price_date:
                    inst.last_price, inst.last_price_date = _d(val["nav"]), vdate
                    inst.save(update_fields=["last_price", "last_price_date"])
            for t in s.get("transactions", []):
                made = _flow(broker if via_zerodha and broker else account, folio["folio"], inst, t)
                if made is None:
                    continue
                created += made
                skipped += not made

    pull = None
    if positions:
        pull = HoldingsPull.objects.create(account=account, source=HoldingsPull.Source.CAS, as_of=as_of,
                                           note=f"CAS {data.get('statement_period', {}).get('to', '')}")
        for inst, units, avg, nav, value in positions:
            pos, new = Position.objects.get_or_create(
                pull=pull, instrument=inst,
                defaults=dict(quantity=units, avg_cost=avg, price=nav, value=value),
            )
            if not new:  # same scheme in two folios
                pos.quantity += units
                pos.value += value
                pos.save()
        pull.total_value = sum(p[4] for p in positions)
        pull.save(update_fields=["total_value"])
    from statements.matching import reconcile
    for acc in filter(None, (account, broker)):
        reconcile(acc)  # drop bank-statement rows that duplicate these purchases
    return pull, created, skipped


def _flow(account, folio, inst, t):
    """Create one CashFlow for a transaction; None if the type isn't money in/out."""
    ttype = _type(t["type"])
    if ttype not in INFLOW | OUTFLOW and ttype != "REVERSAL":
        return None
    amount = abs(_d(t.get("amount")))
    if not amount:
        return None
    if ttype == "REVERSAL":
        sign = -1 if _d(t.get("units")) < 0 else 1
    else:
        sign = 1 if ttype in INFLOW else -1
    signed = amount * sign
    date = _date(t["date"])
    key = hashlib.sha1(f"{folio}|{inst.identifier}|{date}|{ttype}|{signed}|{t.get('units')}".encode()).hexdigest()[:24]
    kind = CashFlow.Kind.SWITCH if ttype in SWITCHES else (
        CashFlow.Kind.INVEST if signed > 0 else CashFlow.Kind.WITHDRAW)
    _, made = CashFlow.objects.get_or_create(
        external_id=f"cas:{key}",
        defaults=dict(date=date, account=account, amount=signed, kind=kind, source=CashFlow.Source.CAS,
                      instrument=inst,
                      note=f"{ttype.title().replace('_', ' ')}: {inst.name[:120]}"),
    )
    return made
