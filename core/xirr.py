"""XIRR: the annualised return that makes the dated cash flows net to zero.
Convention: money you put in is negative, value you hold/take out is positive."""
from decimal import Decimal


def xnpv(rate, flows):
    t0 = flows[0][0]
    return sum(float(a) / (1 + rate) ** ((d - t0).days / 365.0) for d, a in flows)


def xirr(flows, lo=-0.99, hi=10.0, tol=1e-7):
    flows = sorted((d, float(a)) for d, a in flows if a)
    if len(flows) < 2 or not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    f_lo, f_hi = xnpv(lo, flows), xnpv(hi, flows)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = xnpv(mid, flows)
        if abs(f_mid) < tol:
            break
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return Decimal(str(round(mid, 6)))
