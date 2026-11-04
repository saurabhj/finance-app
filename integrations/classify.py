"""Rules that guess an instrument's asset class, whether it's passive, and who manages it.

Defaults (all flagged needs_review so you confirm them once on the Holdings page):
- Stocks in a Zerodha demat -> advisor-managed (they come from Smallcase / Weekend Investing).
- ETFs, SGBs -> self-managed.
- Index funds held via ET Money's CAS folios -> advisor-managed ("ET Money" rebalances them).
- Active mutual funds -> self-managed (Claude gives exit/hold/add calls).
"""
import re

from core.models import AssetClass, Instrument

GOLD = ("GOLD", "SGB", "SILVER")
DEBT = ("LIQUID", "GILT", "BOND", "DEBT", "MONEY MARKET", "OVERNIGHT", "CORPORATE",
        "BANKING & PSU", "BANKING AND PSU", "SHORT DURATION", "ULTRA SHORT", "LOW DURATION",
        "MEDIUM DURATION", "DYNAMIC BOND", "CREDIT RISK", "FLOATER", "TARGET MATURITY", "CPSE BOND",
        "BHARAT BOND", "SDL", "TREASURY")
HYBRID = ("HYBRID", "BALANCED ADVANTAGE", "MULTI ASSET", "ARBITRAGE", "EQUITY SAVINGS",
          "DYNAMIC ASSET", "ASSET ALLOCATION")
PASSIVE = ("INDEX", "NIFTY", "SENSEX", "ETF", "BEES")
DEBT_ETF_SYMBOLS = ("LIQUIDBEES", "LIQUIDCASE", "LIQUIDETF", "GILT5YBEES", "EBBETF", "NETFLTGILT")


def _has(text, words):
    return any(w in text for w in words)


def asset_class_for(name, symbol=""):
    text = f"{symbol} {name}".upper()
    if symbol.upper() in DEBT_ETF_SYMBOLS:
        return AssetClass.DEBT
    if _has(text, GOLD):
        return AssetClass.GOLD
    if _has(text, DEBT):
        return AssetClass.DEBT
    if _has(text, HYBRID):
        return AssetClass.HYBRID
    return AssetClass.EQUITY


def kind_for_symbol(symbol, name=""):
    s = symbol.upper()
    if s.startswith("SGB"):
        return Instrument.Kind.SGB
    if s.endswith("BEES") or "ETF" in s or "ETF" in name.upper() or s.endswith("CASE"):
        return Instrument.Kind.ETF
    return Instrument.Kind.STOCK


def is_passive(name, symbol=""):
    text = f"{symbol} {name}".upper()
    return bool(re.search(r"\b(INDEX|NIFTY|SENSEX)\b", text)) or _has(text, ("ETF", "BEES"))


def default_management(kind, passive, via_et_money=False):
    if kind == Instrument.Kind.STOCK:
        return Instrument.Management.ADVISOR, "Smallcase / Weekend Investing"
    if kind == Instrument.Kind.MF and passive and via_et_money:
        return Instrument.Management.ADVISOR, "ET Money"
    return Instrument.Management.SELF, ""


def get_or_create_instrument(identifier, name, kind, isin="", amfi_code="", via_et_money=False):
    """Find an instrument by ISIN or identifier; create it with guessed tags if new.
    Never overwrites tags you've already confirmed."""
    inst = None
    if isin:
        inst = Instrument.objects.filter(isin=isin).first()
    if inst is None:
        inst = Instrument.objects.filter(identifier=identifier).first()
    if inst:
        changed = False
        for field, val in (("isin", isin), ("amfi_code", amfi_code), ("name", name)):
            if val and not getattr(inst, field):
                setattr(inst, field, val)
                changed = True
        if changed:
            inst.save()
        return inst
    passive = is_passive(name, identifier)
    management, advisor = default_management(kind, passive, via_et_money)
    return Instrument.objects.create(
        identifier=identifier, name=name, kind=kind, isin=isin, amfi_code=amfi_code,
        asset_class=asset_class_for(name, identifier), is_passive=passive,
        management=management, advisor=advisor, needs_review=True,
    )
