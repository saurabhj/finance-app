"""Historical NAVs from mfapi.in (free, no key) for fund-vs-benchmark comparisons."""
import datetime as dt
from decimal import Decimal

import requests

from core.models import NavPrice

URL = "https://api.mfapi.in/mf/{code}"
# Index funds used as benchmark proxies (direct growth plans track the index closely).
BENCHMARKS = {
    "Nifty 50": "120716",  # UTI Nifty 50 Index Fund - Direct Plan - Growth
}
_cache = {}


def history(code):
    """[(date, nav)] oldest first. Cached for the process."""
    if code in _cache:
        return _cache[code]
    resp = requests.get(URL.format(code=code), timeout=30)
    resp.raise_for_status()
    rows = [(dt.datetime.strptime(r["date"], "%d-%m-%Y").date(), Decimal(r["nav"]))
            for r in resp.json().get("data", []) if r.get("nav")]
    rows.sort()
    _cache[code] = rows
    return rows


def store_history(instrument):
    """Save NAV history for a held fund (skips if we already have recent long history)."""
    if not instrument.amfi_code:
        return 0
    rows = history(instrument.amfi_code)
    existing = set(instrument.navs.values_list("date", flat=True))
    new = [NavPrice(instrument=instrument, date=d, nav=n) for d, n in rows if d not in existing]
    NavPrice.objects.bulk_create(new, batch_size=1000)
    return len(new)


def _nav_on_or_before(rows, date):
    best = None
    for d, n in rows:
        if d > date:
            break
        best = n
    return best


def trailing_returns(rows, as_of=None):
    """Point-to-point returns: 1m/6m simple, 1y/3y/5y annualised. None where history is short."""
    if not rows:
        return {}
    as_of = as_of or rows[-1][0]
    end = _nav_on_or_before(rows, as_of)
    out = {}
    for label, days, annualise in (("1m", 30, False), ("6m", 182, False), ("1y", 365, True),
                                   ("3y", 3 * 365, True), ("5y", 5 * 365, True)):
        start_date = as_of - dt.timedelta(days=days)
        if rows[0][0] > start_date:
            out[label] = None
            continue
        start = _nav_on_or_before(rows, start_date)
        r = float(end / start)
        out[label] = round(r ** (365 / days) - 1 if annualise else r - 1, 4)
    return out


def benchmark_returns(name="Nifty 50"):
    try:
        return trailing_returns(history(BENCHMARKS[name]))
    except (requests.RequestException, KeyError, ValueError):
        return {}
