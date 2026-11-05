"""Work expenses paid personally and reimbursed later (e.g. Amazon orders for Flourish on a card,
repaid into the bank). Neither side counts as household spending; what's still owed is tracked."""
import datetime as dt
from collections import defaultdict
from decimal import Decimal

from django.db import transaction as db_tx

from core.models import Category, Transaction

LOOKBACK_DAYS = 60
POSTING_LAG_DAYS = 3  # a card can post a purchase after the repayment has already landed
ZERO = Decimal("0")


@db_tx.atomic
def pair_reimbursements():
    """Match each unpaired reimbursement to a purchase of the exact same amount made up to 60 days
    earlier (any bank or card), and tag that purchase as reimbursable. Returns pairs made."""
    made = 0
    for r in Transaction.objects.filter(category=Category.REIMBURSEMENT, matched_with__isnull=True,
                                        amount__gt=0).order_by("date"):
        window = (r.date - dt.timedelta(days=LOOKBACK_DAYS), r.date + dt.timedelta(days=POSTING_LAG_DAYS))
        cands = (Transaction.objects.filter(amount__gte=-r.amount - 1, amount__lte=-r.amount + 1, date__range=window,
                                            matched_with__isnull=True)
                 .filter(category__in=["", Category.EXPENSE, Category.REIMBURSABLE])
                 .exclude(pk=r.pk).order_by("-date"))
        # prefer purchases already marked reimbursable, then the closest in time
        purchase = cands.filter(category=Category.REIMBURSABLE).first() or cands.first()
        if not purchase:
            continue
        purchase.category, purchase.spend_category = Category.REIMBURSABLE, ""
        purchase.party = purchase.party or r.party
        if purchase.source != Transaction.Source.USER:
            purchase.source = Transaction.Source.MATCH
        purchase.matched_with = r
        purchase.save()
        r.matched_with = purchase
        r.save(update_fields=["matched_with"])
        made += 1
    return made


def summary():
    """Per party: spent on their behalf, received back, still owed; plus unpaired rows on each side."""
    rows = defaultdict(lambda: {"spent": ZERO, "received": ZERO, "unpaid": [], "unmatched_received": []})
    qs = Transaction.objects.filter(category__in=[Category.REIMBURSABLE, Category.REIMBURSEMENT]).select_related("account")
    for t in qs.order_by("date"):
        p = rows[t.party or "Unassigned"]
        if t.category == Category.REIMBURSABLE:
            p["spent"] += -t.amount
            if not t.matched_with_id:
                p["unpaid"].append(t)
        else:
            p["received"] += t.amount
            if not t.matched_with_id:
                p["unmatched_received"].append(t)
    out = []
    for party, p in sorted(rows.items()):
        out.append({"party": party, **p, "owed": p["spent"] - p["received"]})
    return out
