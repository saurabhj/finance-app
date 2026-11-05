"""Local parser for CSV / Excel statements (any bank, by detecting the header row)."""
import csv
import datetime as dt
import io
import re
from decimal import Decimal, InvalidOperation


class TableParseError(Exception):
    pass


DATE_COLS = ["transaction date", "txn date", "tran date", "date", "posting date", "value date"]
DESC_COLS = ["narration", "description", "particulars", "transaction details", "transaction remarks",
             "details", "remarks", "merchant", "transaction description"]
DEBIT_COLS = ["withdrawal amt", "withdrawal amount", "withdrawal", "withdrawals", "debit amount", "debit", "dr", "amount dr"]
CREDIT_COLS = ["deposit amt", "deposit amount", "deposit", "deposits", "credit amount", "credit", "cr", "amount cr"]
AMOUNT_COLS = ["amount", "transaction amount", "amount (inr)", "amount(inr)", "amt"]
TYPE_COLS = ["debit / credit", "debit /credit", "debit/credit","dr/cr", "cr/dr", "billingamountsign", "cr/dr indicator", "type"]
BALANCE_COLS = ["closing balance", "balance", "available balance", "running balance"]

DATE_FORMATS = ["%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %b %y",
                "%Y-%m-%d", "%d.%m.%Y", "%d %B %Y", "%b %d, %Y", "%d/%b/%Y"]


def rows_from_file(name, data):
    lower = name.lower()
    if lower.endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        return [list(r) for ws in wb.worksheets for r in ws.iter_rows(values_only=True)]
    if lower.endswith(".xls"):
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=data)
            rows = []
            for sh in book.sheets():
                for i in range(sh.nrows):
                    row = []
                    for c in sh.row(i):
                        if c.ctype == xlrd.XL_CELL_DATE:
                            row.append(xlrd.xldate.xldate_as_datetime(c.value, book.datemode))
                        else:
                            row.append(c.value)
                    rows.append(row)
            return rows
        except Exception as e:  # many banks' ".xls" are really HTML
            raise TableParseError(f"Couldn't read .xls ({e})") from e
    text = data.decode("utf-8-sig", errors="replace")
    if "~|~" in text:  # HDFC card exports use "~|~" between fields
        text = text.replace("~|~", "\t").replace("~\r", "\r").replace("~\n", "\n")
        return [[c.strip().strip("~") for c in line.split("\t")] for line in text.splitlines()]
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


def _norm(v):
    return re.sub(r"\s+", " ", str(v or "")).strip().lower().replace(".", "").replace("(₹)", "").replace("(rs)", "").strip()


def _find(header, names):
    for n in names:
        for i, h in enumerate(header):
            if h == n:
                return i
    for n in names:
        for i, h in enumerate(header):
            if h.startswith(n) and len(n) > 3:
                return i
    return None


def find_header(rows):
    for idx, row in enumerate(rows[:60]):
        header = [_norm(c) for c in row]
        d, desc = _find(header, DATE_COLS), _find(header, DESC_COLS)
        if d is None or desc is None:
            continue
        cols = {"date": d, "desc": desc, "debit": _find(header, DEBIT_COLS), "credit": _find(header, CREDIT_COLS),
                "amount": _find(header, AMOUNT_COLS), "type": _find(header, TYPE_COLS),
                "balance": _find(header, BALANCE_COLS)}
        for side in ("debit", "credit"):  # a "Debit / Credit" marker column is not an amount column
            if cols[side] == cols["type"]:
                cols[side] = None
        if (cols["debit"] is not None and cols["credit"] is not None) or cols["amount"] is not None:
            return idx, cols
    raise TableParseError("Couldn't find a header row with date, description and amount columns.")


def parse_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v or "").strip()
    for candidate in (s, s.split(" ")[0] if " " in s else None):  # drop a trailing time, e.g. "06/09/2026 09:09:54"
        if not candidate:
            continue
        for fmt in DATE_FORMATS:
            try:
                return dt.datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def parse_amount(v):
    """Returns (Decimal|None, 'cr'|'dr'|None) from values like '1,234.50', '1234.5 Cr', '(500)'."""
    if v in (None, ""):
        return None, None
    if isinstance(v, (int, float)):
        return Decimal(str(v)), None
    s = str(v).strip().upper().replace(",", "").replace("₹", "").replace("INR", "").replace("RS.", "").strip()
    hint = "cr" if s.endswith("CR") else "dr" if s.endswith("DR") else None
    s = s.removesuffix("CR").removesuffix("DR").strip()
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = s.strip("()-+ ")
    if not s:
        return None, hint
    try:
        val = Decimal(s)
    except InvalidOperation:
        return None, hint
    return (-val if neg else val), hint


PERIOD_RE = re.compile(r"from\W{0,6}(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}).{0,30}?to\W{0,6}(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", re.I)


STATEMENT_DATE_RE = re.compile(r"statement date\W{0,6}(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}[ -][a-z]{3,9},?[ -]\d{2,4})", re.I)


def find_period(rows, upto, is_card=False):
    """Printed statement period, e.g. 'Statement From : 01/10/2026 To : 31/10/2026'.
    For cards that only print a statement date, assume the month-long billing cycle ending on it."""
    lines = [" ".join(str(c) for c in row if c not in (None, "")) for row in rows[:upto]]
    for line in lines:
        m = PERIOD_RE.search(line)
        if m:
            a, b = parse_date(m.group(1)), parse_date(m.group(2))
            if a and b and a <= b:
                return a, b
    if is_card:
        for line in lines:
            m = STATEMENT_DATE_RE.search(line)
            end = parse_date(m.group(1).replace(",", "")) if m else None
            if end:
                prev = (end.replace(day=1) - dt.timedelta(days=1))
                start = prev.replace(day=min(end.day, 28)) + dt.timedelta(days=1)
                return start, end
    return None, None


def parse(name, data, is_card, with_period=False):
    """Returns list of dicts {date, description, amount (+in/-out), balance}."""
    rows = rows_from_file(name, data)
    start, c = find_header(rows)
    out = []
    for row in rows[start + 1:]:
        def cell(key):
            i = c.get(key)
            return row[i] if i is not None and i < len(row) else None

        date = parse_date(cell("date"))
        desc = re.sub(r"\s+", " ", str(cell("desc") or "")).strip()
        if not date or not desc:
            continue
        if c["debit"] is not None and c["credit"] is not None:
            dr, _ = parse_amount(cell("debit"))
            cr, _ = parse_amount(cell("credit"))
            amount = (cr or 0) - (dr or 0)
        else:
            val, hint = parse_amount(cell("amount"))
            if val is None:
                continue
            t = _norm(cell("type"))
            if t.startswith("cr") or hint == "cr" or t == "credit":
                amount = abs(val)
            elif t.startswith("dr") or hint == "dr" or t == "debit":
                amount = -abs(val)
            elif is_card:
                amount = -val  # card exports list spends as positive numbers
            else:
                raise TableParseError("Single amount column with no debit/credit marker; can't tell direction.")
        if not amount:
            continue
        bal, _ = parse_amount(cell("balance"))
        out.append({"date": date, "description": desc[:300], "amount": Decimal(amount), "balance": bal})
    if not out:
        raise TableParseError("No transactions found under the header row.")
    return (out, find_period(rows, start, is_card)) if with_period else out
