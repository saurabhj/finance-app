"""Daily mutual fund NAVs from AMFI (free, no key), used to revalue MF holdings
between CAS statements and, later, to compare funds against their benchmarks."""
import datetime as dt
from decimal import Decimal, InvalidOperation

import requests
from django.db.models import Q

from core.models import Instrument, NavPrice

NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"


def parse(text):
    """Yield (scheme_code, isins, name, nav, date) from AMFI's NAVAll.txt."""
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        # 6 columns (older) or 8 with Plan and Option before the NAV (current layout)
        code, isin1, isin2 = parts[:3]
        name = " - ".join(x for x in parts[3:-2] if x)
        nav, date = parts[-2], parts[-1]
        try:
            nav = Decimal(nav)
            date = dt.datetime.strptime(date, "%d-%b-%Y").date()
        except (InvalidOperation, ValueError):
            continue
        isins = {i for i in (isin1, isin2) if i and i != "-"}
        yield code, isins, name, nav, date


def update_navs(text=None):
    """Update last_price and NAV history for every MF instrument we hold. Returns count updated."""
    if text is None:
        resp = requests.get(NAV_URL, timeout=60)
        resp.raise_for_status()
        text = resp.text
    tracked = Instrument.objects.filter(Q(kind=Instrument.Kind.MF) | ~Q(amfi_code=""))
    by_isin = {i.isin: i for i in tracked if i.isin}
    by_code = {i.amfi_code: i for i in tracked if i.amfi_code}
    updated = 0
    for code, isins, name, nav, date in parse(text):
        inst = by_code.get(code) or next((by_isin[i] for i in isins if i in by_isin), None)
        if inst is None:
            continue
        NavPrice.objects.update_or_create(instrument=inst, date=date, defaults={"nav": nav})
        if not inst.last_price_date or date >= inst.last_price_date:
            inst.last_price, inst.last_price_date = nav, date
            if not inst.amfi_code:
                inst.amfi_code = code
            inst.save(update_fields=["last_price", "last_price_date", "amfi_code"])
            updated += 1
    return updated
