"""Import history from the hand-maintained net_worth.xlsx.

Summary sheet layout: row 1 = person blocks, row 2 = account labels, row 3 =
expected returns, rows 4+ = one dated row per month. Saurabh's accounts sit in
columns B..M, Pritika's in O..V, totals in X (Saurabh), Z (Pritika), AB (all).
Older dated tabs ("5 July 2024", ...) list "label | value" per person.
"""
import datetime as dt
from decimal import Decimal

import openpyxl
from django.db import transaction

from .models import Account, AssetClass, Balance, Person, Snapshot

K = Account.Kind
# label -> (kind, asset_class, institution, canonical name)
ACCOUNT_META = {
    "Zerodha": (K.BROKER, AssetClass.EQUITY, "Zerodha", "Zerodha"),
    "ET Money": (K.MF, AssetClass.EQUITY, "ET Money", "ET Money"),
    "MF (ET Money)": (K.MF, AssetClass.EQUITY, "ET Money", "ET Money"),
    "MF": (K.MF, AssetClass.EQUITY, "ET Money", "ET Money"),
    "FDs": (K.FD, AssetClass.DEBT, "", "FDs"),
    "ICICI FD": (K.FD, AssetClass.DEBT, "ICICI", "ICICI FD"),
    "ULIP": (K.ULIP, AssetClass.HYBRID, "", "ULIP"),
    "Falcon": (K.OTHER, AssetClass.DEBT, "Falcon", "Falcon"),
    "Bank ICICI": (K.BANK, AssetClass.CASH, "ICICI", "Bank ICICI"),
    "Bank": (K.BANK, AssetClass.CASH, "ICICI", "Bank ICICI"),  # old tabs: assumed ICICI
    "Bank Kotak": (K.BANK, AssetClass.CASH, "Kotak", "Bank Kotak"),
    "Bank HDFC": (K.BANK, AssetClass.CASH, "HDFC", "Bank HDFC"),
    "Bank SC": (K.BANK, AssetClass.CASH, "Standard Chartered", "Bank SC"),
}
SUMMARY_TOTAL_COL = 27  # AB
PRITIKA_START_COL = 14  # O
TOTALS_START_COL = 23   # X
OLD_TABS = ["5 July 2024", "13 Aug 2024"]


def _dec(v):
    return Decimal(str(round(float(v), 2)))


def _account(person, label, expected_return=None, order=0):
    kind, asset_class, institution, name = ACCOUNT_META[label]
    acc, created = Account.objects.get_or_create(
        owner=person, name=name,
        defaults=dict(kind=kind, asset_class=asset_class, institution=institution, sort_order=order),
    )
    if expected_return is not None and not acc.expected_return:
        acc.expected_return = _dec(expected_return).quantize(Decimal("0.0001"))
        acc.save(update_fields=["expected_return"])
    return acc


@transaction.atomic
def import_workbook(path):
    """Returns a list of (date, imported_total, sheet_total) for checking."""
    wb = openpyxl.load_workbook(path, data_only=True)
    saurabh, _ = Person.objects.get_or_create(name="Saurabh")
    pritika, _ = Person.objects.get_or_create(name="Pritika")
    report = []

    ws = wb["Summary"]
    rows = list(ws.iter_rows(values_only=True))
    labels, rates = rows[1], rows[2]
    columns = []  # (col index, Account)
    for idx, label in enumerate(labels[:TOTALS_START_COL]):
        if not label or label not in ACCOUNT_META:
            continue
        person = saurabh if idx < PRITIKA_START_COL else pritika
        rate = rates[idx] if isinstance(rates[idx], (int, float)) else None
        columns.append((idx, _account(person, label, rate, order=idx)))

    for row in rows[3:]:
        if not isinstance(row[0], dt.datetime):
            continue
        snap = _snapshot(row[0].date())
        total = Decimal("0")
        for idx, acc in columns:
            v = row[idx]
            if isinstance(v, (int, float)):
                Balance.objects.create(snapshot=snap, account=acc, value=_dec(v))
                total += _dec(v)
        sheet_total = row[SUMMARY_TOTAL_COL]
        report.append((snap.date, total, _dec(sheet_total) if sheet_total else None))

    for tab in OLD_TABS:
        if tab not in wb.sheetnames:
            continue
        report.append(_import_old_tab(wb[tab], saurabh, pritika))
    # Accounts with no balance in the latest snapshot (e.g. Falcon, 2024 only) are hidden from entry forms.
    latest = Snapshot.objects.order_by("-date").first()
    held = set(latest.balances.values_list("account_id", flat=True))
    Account.objects.exclude(id__in=held).exclude(kind=K.LOAN).update(is_active=False)
    return sorted(report)


def _snapshot(date):
    Snapshot.objects.filter(date=date).delete()
    return Snapshot.objects.create(
        date=date, source=Snapshot.Source.IMPORT, flows_tracked=False,
        note="Imported from net_worth.xlsx",
    )


def _import_old_tab(ws, saurabh, pritika):
    rows = list(ws.iter_rows(values_only=True))
    snap = _snapshot(rows[0][0].date())
    sheet_total = _dec(rows[1][4]) if rows[1][4] else None
    person, total = None, Decimal("0")
    for row in rows[1:]:
        label, value = row[0], row[1]
        if label in ("Saurabh", "Pritika"):
            person = saurabh if label == "Saurabh" else pritika
            continue
        if label in ACCOUNT_META and isinstance(value, (int, float)):
            acc = _account(person, label)
            bal, created = Balance.objects.get_or_create(
                snapshot=snap, account=acc, defaults={"value": _dec(value)}
            )
            if not created:  # two old labels mapped to one account
                bal.value += _dec(value)
                bal.save()
            total += _dec(value)
    return (snap.date, total, sheet_total)
