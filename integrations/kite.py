"""Zerodha Kite Connect: read-only holdings pull for each person's demat.

Setup per Zerodha login (Saurabh, Pritika): create an app at developers.kite.trade,
set its redirect URL to  http://<this host>:8765/zerodha/callback/  and put the
key/secret in .env as KITE_API_KEY_<NAME> / KITE_API_SECRET_<NAME>.

Kite access tokens expire every morning (~6am IST), so each pull starts with a
login on Kite's own page. This module never places orders.
"""
import datetime as dt
import os
from decimal import Decimal
from urllib.parse import urlencode

from django.db import transaction
from django.utils import timezone

from core.models import Account, AssetClass, HoldingsPull, Instrument, KiteSession, Person, Position

from .classify import get_or_create_instrument, kind_for_symbol

CASH_ID = "CASH-ZERODHA"


class KiteNotConfigured(Exception):
    pass


class KiteLoginRequired(Exception):
    pass


def _env_name(person):
    return person.name.upper().replace(" ", "_")


def credentials(person):
    key = os.environ.get(f"KITE_API_KEY_{_env_name(person)}")
    secret = os.environ.get(f"KITE_API_SECRET_{_env_name(person)}")
    if not key or not secret:
        raise KiteNotConfigured(
            f"Add KITE_API_KEY_{_env_name(person)} and KITE_API_SECRET_{_env_name(person)} to .env"
        )
    return key, secret


def is_configured(person):
    try:
        credentials(person)
        return True
    except KiteNotConfigured:
        return False


def _kite(api_key, access_token=None):
    from kiteconnect import KiteConnect

    k = KiteConnect(api_key=api_key)
    if access_token:
        k.set_access_token(access_token)
    return k


def login_url(person):
    key, _ = credentials(person)
    return _kite(key).login_url() + "&" + urlencode({"redirect_params": urlencode({"person": person.id})})


def complete_login(person, request_token):
    key, secret = credentials(person)
    data = _kite(key).generate_session(request_token, api_secret=secret)
    KiteSession.objects.update_or_create(
        person=person,
        defaults={"access_token": data["access_token"], "kite_user_id": data.get("user_id", ""),
                  "created_at": timezone.now()},
    )


def _token_valid(session):
    """Kite tokens die at ~6am IST the day after login."""
    now = timezone.localtime()
    expiry = timezone.localtime(session.created_at).replace(hour=6, minute=0, second=0, microsecond=0)
    if timezone.localtime(session.created_at) >= expiry:
        expiry += dt.timedelta(days=1)
    return now < expiry


def client(person):
    session = KiteSession.objects.filter(person=person).first()
    if not session or not _token_valid(session):
        raise KiteLoginRequired(person.name)
    key, _ = credentials(person)
    return _kite(key, session.access_token)


def zerodha_account(person):
    acc = Account.objects.filter(owner=person, kind=Account.Kind.BROKER, institution__iexact="Zerodha").first()
    if acc is None:
        acc = Account.objects.create(owner=person, name="Zerodha", institution="Zerodha",
                                     kind=Account.Kind.BROKER, asset_class=AssetClass.EQUITY)
    return acc


def _d(v):
    return Decimal(str(v or 0))


@transaction.atomic
def pull_holdings(person, kite=None):
    """Fetch demat holdings, Coin mutual funds and idle cash; store as a HoldingsPull."""
    kite = kite or client(person)
    account = zerodha_account(person)
    pull = HoldingsPull.objects.create(account=account, source=HoldingsPull.Source.KITE,
                                       as_of=timezone.localdate())
    total = Decimal("0")

    for h in kite.holdings():
        qty = _d(h.get("quantity")) + _d(h.get("t1_quantity"))
        if qty <= 0:
            continue
        symbol = h["tradingsymbol"]
        inst = get_or_create_instrument(symbol, symbol, kind_for_symbol(symbol), isin=h.get("isin", ""))
        price = _d(h.get("last_price")) or _d(h.get("close_price"))
        value = (qty * price).quantize(Decimal("0.01"))
        _add(pull, inst, qty, _d(h.get("average_price")), price, value)
        total += value

    for m in kite.mf_holdings():
        qty = _d(m.get("quantity"))
        if qty <= 0:
            continue
        isin = m.get("tradingsymbol", "")
        inst = get_or_create_instrument(isin, m.get("fund", isin), Instrument.Kind.MF, isin=isin)
        price = _d(m.get("last_price"))
        value = (qty * price).quantize(Decimal("0.01"))
        _add(pull, inst, qty, _d(m.get("average_price")), price, value)
        total += value

    cash = _d(kite.margins().get("equity", {}).get("net"))
    if cash > 0:
        inst, _ = Instrument.objects.get_or_create(
            identifier=CASH_ID,
            defaults={"name": "Cash in Zerodha", "kind": Instrument.Kind.OTHER,
                      "asset_class": AssetClass.CASH, "needs_review": False},
        )
        _add(pull, inst, Decimal("1"), None, cash, cash)
        total += cash

    pull.total_value = total
    pull.save(update_fields=["total_value"])
    return pull


def _add(pull, inst, qty, avg, price, value):
    pos, created = Position.objects.get_or_create(
        pull=pull, instrument=inst,
        defaults={"quantity": qty, "avg_cost": avg or None, "price": price, "value": value},
    )
    if not created:  # same ISIN in demat and Coin
        pos.quantity += qty
        pos.value += value
        pos.save()


def people_status():
    out = []
    for p in Person.objects.people():
        session = KiteSession.objects.filter(person=p).first()
        out.append({
            "person": p,
            "configured": is_configured(p),
            "connected": bool(session and _token_valid(session)),
            "last_pull": HoldingsPull.objects.filter(account__owner=p, source="kite").first(),
        })
    return out
