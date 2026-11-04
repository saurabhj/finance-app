from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def inr(value, signed=False):
    """Indian short form: ₹6.71 Cr, ₹12.3 L, ₹45,000."""
    if value is None or value == "":
        return "—"
    v = Decimal(value)
    sign = "−" if v < 0 else ("+" if signed and v > 0 else "")
    a = abs(v)
    if a >= 10_000_000:
        s = f"{a / 10_000_000:.2f} Cr"
    elif a >= 100_000:
        s = f"{a / 100_000:.2f} L"
    else:
        s = f"{a:,.0f}"
    return f"{sign}₹{s}"


@register.filter
def inr_signed(value):
    return inr(value, signed=True)


@register.filter
def pct(value, digits=1):
    if value is None:
        return "—"
    return f"{Decimal(value) * 100:+.{int(digits)}f}%"


@register.filter
def pct_plain(value):
    if value is None:
        return "—"
    return f"{Decimal(value) * 100:.1f}%"


@register.filter
def tone(value):
    """CSS class for a signed number."""
    if value is None:
        return "text-muted"
    return "text-success" if value > 0 else ("text-danger" if value < 0 else "")


@register.filter
def get_item(d, key):
    return d.get(key) if d else None


@register.filter
def ratio(a, b):
    try:
        return Decimal(a) / Decimal(b) if b else None
    except Exception:
        return None


@register.filter
def gain_pct(pos):
    """Unrealised gain % of a Position vs its average cost."""
    if not pos.avg_cost:
        return None
    cost = pos.avg_cost * pos.quantity
    return (pos.value - cost) / cost if cost else None
