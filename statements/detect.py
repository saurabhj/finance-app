"""Work out which account a statement file belongs to, from numbers printed on it."""
import io
import re

from core.models import Account

from . import parse_table


def pdf_password(data, passwords):
    """The first password that opens the PDF ('' if it isn't encrypted); None if none work."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if not reader.is_encrypted:
        return ""
    for pw in passwords:
        try:
            if reader.decrypt(pw):
                return pw
        except Exception:  # noqa: BLE001 - pypdf raises various errors on bad passwords
            continue
    return None


def header_text(name, data, password=""):
    """Text from the top of the statement, where account and card numbers are printed."""
    if name.lower().endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt(password or "")
        return " ".join((reader.pages[i].extract_text() or "") for i in range(min(2, len(reader.pages))))[:6000]
    try:
        rows = parse_table.rows_from_file(name, data)
        return "\n".join(" ".join(str(c) for c in r if c not in (None, "")) for r in rows[:40])[:6000]
    except Exception:  # noqa: BLE001
        return data[:6000].decode("utf-8", errors="replace")


def _hint_re(hint):
    digits = re.sub(r"\D", "", hint)
    if not digits:
        return re.compile(re.escape(hint), re.I)
    # the number as a standalone token: not inside a longer number and not an amount like 1234.00
    return re.compile(r"(?<![\d,.])" + r"[\s-]?".join(digits) + r"(?![\d]|[.,]\d)")


def detect(name, data, password="", accounts=None):
    """Returns (account or None, reason). Ambiguous or unknown -> None."""
    accounts = list(accounts if accounts is not None else
                    Account.objects.filter(is_active=True, kind__in=["bank", "card"]).exclude(match_hints=""))
    text = header_text(name, data, password)
    scores = []
    for acc in accounts:
        hits = [h.strip() for h in acc.match_hints.split(",") if h.strip() and _hint_re(h.strip()).search(text)]
        if hits:
            scores.append((max(len(re.sub(r"\D", "", h)) for h in hits), acc, hits))
    if not scores:
        return None, "no account number recognised"
    scores.sort(key=lambda s: -s[0])
    if len(scores) > 1 and scores[0][0] == scores[1][0]:
        return None, "matches more than one account: " + ", ".join(str(s[1]) for s in scores[:3])
    best = scores[0]
    return best[1], f"found {best[2][0]}"
