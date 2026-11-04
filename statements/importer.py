"""Statement upload pipeline: parse -> dedupe -> rules -> Claude guesses -> transfer pairing."""
import hashlib
import re
from collections import Counter
from decimal import Decimal

from django.db import transaction as db_tx

from core.models import Account, StatementUpload, Transaction

from . import matching, parse_ai, parse_table, rules


class ImportError_(Exception):
    pass


def _norm_desc(d):
    return re.sub(r"\s+", " ", d.upper()).strip()


def read(account, name, data, password="", client=None):
    """Returns (rows, parser, warning, period). rows: [{date, description, amount, balance, category?, spend?}]"""
    is_card = account.kind == Account.Kind.CARD
    names = rules.family_names()
    if name.lower().endswith(".pdf"):
        result, warning = parse_ai.extract_pdf(data, password, names, client)
        return _rows_from_ai(result), "claude", warning, _period(result)
    try:
        rows, period = parse_table.parse(name, data, is_card, with_period=True)
        return rows, "table", balance_warning(rows), period
    except parse_table.TableParseError as e:
        text = data.decode("utf-8", errors="replace")
        if "<table" in text.lower():  # HTML disguised as .xls
            text = re.sub(r"<[^>]+>", " ", re.sub(r"</t[dh]>", "\t", re.sub(r"</tr>", "\n", text, flags=re.I), flags=re.I))
        result, warning = parse_ai.extract_text(text, names, client)
        return _rows_from_ai(result), "claude", f"Local parser failed ({e}); Claude read it instead. {warning}".strip(), _period(result)


def balance_warning(rows):
    """Running balance should move by exactly each amount (rows may be oldest- or newest-first)."""
    bal = [(r["amount"], r["balance"]) for r in rows if r.get("balance") is not None]
    if len(bal) < 2:
        return ""

    def breaks(seq):
        return sum(1 for (_, b0), (a1, b1) in zip(seq, seq[1:]) if abs(b0 + a1 - b1) > Decimal("0.01"))

    n = min(breaks(bal), breaks(list(reversed(bal))))
    return f"Running balance doesn't add up at {n} row(s); some transactions may be missing or misread." if n else ""


def _period(result):
    import datetime as dt
    out = []
    for k in ("period_start", "period_end"):
        try:
            out.append(dt.date.fromisoformat(result.get(k) or ""))
        except ValueError:
            out.append(None)
    return tuple(out)


def _rows_from_ai(result):
    import datetime as dt
    rows = []
    for t in result["transactions"]:
        try:
            date = dt.date.fromisoformat(t["date"])
        except ValueError:
            continue
        amt = Decimal(str(t["amount"]))
        rows.append({"date": date, "description": t["description"][:300],
                     "amount": amt if t["direction"] == "credit" else -amt,
                     "balance": Decimal(str(t["balance"])) if t["balance"] else None,
                     "category": t["category"], "spend": t["spend_category"]})
    return rows


def guess_investment_account(t):
    """Only when the description names the platform; otherwise leave it for you to pick."""
    d = t.description.upper()
    for words, target in ((("ZERODHA",), "zerodha"), (("ET MONEY", "ETMONEY", "ICCL", "BSE STAR"), "mf"),
                          (("IWISH", "FD ", "FIXED DEP", "RECURRING"), "fd")):
        if any(w in d for w in words):
            return rules.resolve_target(target, t.account.owner)
    return None


@db_tx.atomic
def reapply_rules():
    """Re-run rules over unconfirmed rows (after adding or fixing rules). Your own edits are kept."""
    rule_list, changed = rules.ordered_rules(), 0
    for t in Transaction.objects.filter(confirmed=False).exclude(source=Transaction.Source.USER).select_related("account__owner"):
        rule = rules.match_rule(rule_list, t.description, t.amount, t.account)
        before = (t.category, t.spend_category, t.investment_account_id)
        if rule:
            rules.apply_rule(t, rule)
        elif t.category in ("investment", "inv_return") and t.source != "rule":
            t.investment_account = guess_investment_account(t)
        if (t.category, t.spend_category, t.investment_account_id) != before:
            t.save()
            changed += 1
    from .reimburse import pair_reimbursements
    pair_reimbursements()
    return changed


@db_tx.atomic
def import_statement(account, name, data, password="", client=None, pre_read=None):
    """pre_read: the result of read() done earlier (e.g. in a worker thread)."""
    sha = hashlib.sha256(data).hexdigest()
    if StatementUpload.objects.filter(account=account, sha256=sha).exists():
        raise ImportError_("This exact file was already uploaded for this account.")
    rows, parser, warning, (p_start, p_end) = pre_read or read(account, name, data, password, client)
    if not rows:
        raise ImportError_("No transactions found in that file.")
    dates = [r["date"] for r in rows]
    stmt = StatementUpload.objects.create(
        account=account, filename=name[:200], sha256=sha, parser=parser, warning=warning[:2000],
        period_start=p_start or min(dates), period_end=p_end or max(dates),
    )
    seen = Counter()
    rule_list = rules.ordered_rules()
    names = rules.family_names()
    new, dupes = [], 0
    for r in rows:
        base = f"{account.id}|{r['date']}|{r['amount']}|{_norm_desc(r['description'])}"
        seen[base] += 1
        key = hashlib.sha256(f"{base}|{seen[base]}".encode()).hexdigest()
        if Transaction.objects.filter(dedupe_key=key).exists():
            dupes += 1
            continue
        t = Transaction(statement=stmt, account=account, date=r["date"], description=r["description"],
                        amount=r["amount"], balance=r.get("balance"), dedupe_key=key)
        rule = rules.match_rule(rule_list, t.description, t.amount, account)
        if rule:
            rules.apply_rule(t, rule)
            rule.hits += 1
            rule.save(update_fields=["hits"])
        elif r.get("category"):
            t.category, t.spend_category, t.source = r["category"], r.get("spend") or "", Transaction.Source.AI
        matching.guess_family_transfer(t, names)
        new.append(t)

    for t in new:
        t.save()
    # Pair transfers / card payments first (no AI needed), then ask Claude about what's left.
    matching.pair_transfers(new)
    todo = [t for t in new if not t.category]
    if todo:
        guesses = parse_ai.categorize(todo, account.get_kind_display(), names, client)
        for i, t in enumerate(todo):
            if i in guesses:
                t.category, t.spend_category = guesses[i]
                t.source = Transaction.Source.AI
    for t in new:
        if t.category in ("investment", "inv_return") and not t.investment_account_id:
            t.investment_account = guess_investment_account(t)
        if t.category != "expense":
            t.spend_category = ""
        t.save()
    matching.pair_transfers([t for t in todo if t.category])
    from .reimburse import pair_reimbursements
    pair_reimbursements()
    stmt.added, stmt.duplicates = len(new), dupes
    stmt.save(update_fields=["added", "duplicates"])
    return stmt
