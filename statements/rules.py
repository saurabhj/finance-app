"""Keyword rules: built-in ones for common Indian bank/card descriptions, plus rules you teach it."""
import re

from core.models import Account, Category, CategoryRule, Person, SpendCategory

C, S, T = Category, SpendCategory, CategoryRule.Target

# (keywords, applies_to, direction, category, spend_category, target) in priority order
BUILTIN = [
    # Advisory subscriptions are spending, not investments
    (["WEEKEND INVESTING", "SMALLCASE"], "any", "debit", C.EXPENSE, S.SUBSCRIPTIONS, ""),
    (["ZERODHA"], "bank", "debit", C.INVESTMENT, "", T.ZERODHA),
    (["ZERODHA"], "bank", "credit", C.INVESTMENT_RETURN, "", T.ZERODHA),
    (["ET MONEY", "ETMONEY", "ICCL", "INDIAN CLEARING", "BSE STAR", "BSESTARMF"], "bank", "debit", C.INVESTMENT, "", T.MF),
    (["ET MONEY", "ETMONEY", "ICCL", "INDIAN CLEARING", "BSE STAR", "BSESTARMF"], "bank", "credit", C.INVESTMENT_RETURN, "", T.MF),
    (["FD BOOKED", "TO FD", "FIXED DEPOSIT", "TERM DEPOSIT", "TD BOOKING"], "bank", "debit", C.INVESTMENT, "", T.FD),
    (["FD CLOSURE", "TD CLOSURE", "CLOSURE PROCEEDS", "MATURITY PROCEEDS", "FD PREMAT"], "bank", "credit", C.INVESTMENT_RETURN, "", T.FD),
    (["CRED CLUB", "CREDCLUB", "CRED.CLUB", "CREDIT CARD", "CC PAYMENT", "CARD BILL", "CC BILLP", "CCBILLPAY"], "bank", "debit", C.CARD_PAYMENT, "", ""),
    (["PAYMENT RECEIVED", "THANK YOU", "BBPS PAYMENT", "PAYMENT - THANK", "AUTOPAY"], "card", "credit", C.CARD_PAYMENT, "", ""),
    (["REIMB"], "bank", "credit", C.REIMBURSEMENT, "", ""),
    (["REFUND", "REVERSAL", "CASHBACK"], "any", "credit", C.REFUND, "", ""),
    (["HOME LOAN EMI", "HOUSING LOAN", "LOAN EMI", "HL EMI", "EMI FOR LOAN", "AUTO LOAN", "CAR LOAN", "PERSONAL LOAN"], "bank", "debit", C.LOAN_EMI, "", ""),
    (["IWISH", "RECURRING DEPOSIT", "RD INSTAL"], "bank", "debit", C.INVESTMENT, "", T.FD),
    (["SALARY", "SAL CREDIT", "PAYROLL"], "bank", "credit", C.INCOME, "", ""),
    (["INT.PD", "INT PD", "INTEREST PAID", "INTEREST CREDIT", "CREDIT INTEREST", "SB INT", "DIVIDEND", "ACH C-"],
     "bank", "credit", C.INCOME, "", ""),
    # Merchants (UPI from the bank or card swipes)
    (["SWIGGY", "ZOMATO", "EATSURE", "DOMINOS", "STARBUCKS", "MCDONALD"], "any", "debit", C.EXPENSE, S.DINING, ""),
    (["BIGBASKET", "BLINKIT", "ZEPTO", "INSTAMART", "DMART", "NATURE S BASKET", "NATURES BASKET", "JIOMART"], "any", "debit", C.EXPENSE, S.GROCERIES, ""),
    (["AMAZON", "FLIPKART", "MYNTRA", "AJIO", "NYKAA", "TATA CLIQ", "IKEA", "DECATHLON"], "any", "debit", C.EXPENSE, S.SHOPPING, ""),
    (["UBER", "OLA ", "OLACABS", "RAPIDO", "HPCL", "IOCL", "BPCL", "FUEL", "PETROL", "FASTAG", "METRO"], "any", "debit", C.EXPENSE, S.TRANSPORT, ""),
    (["MAKEMYTRIP", "GOIBIBO", "INDIGO", "AIR INDIA", "VISTARA", "IRCTC", "AIRBNB", "AGODA", "BOOKING.COM", "CLEARTRIP", "TAJ ", "MARRIOTT"], "any", "debit", C.EXPENSE, S.TRAVEL, ""),
    (["NETFLIX", "SPOTIFY", "YOUTUBE", "APPLE.COM", "GOOGLE PLAY", "HOTSTAR", "PRIME VIDEO", "ADOBE", "OPENAI", "ANTHROPIC"], "any", "debit", C.EXPENSE, S.SUBSCRIPTIONS, ""),
    (["AIRTEL", "JIO", "VODAFONE", "VI ", "ELECTRICITY", "BESCOM", "TATA POWER", "ADANI ELEC", "MSEDCL", "MAHANAGAR GAS", "BROADBAND", "ACT FIBERNET"], "any", "debit", C.EXPENSE, S.UTILITIES, ""),
    (["APOLLO", "PHARM", "1MG", "TATA 1MG", "HOSPITAL", "CLINIC", "DIAGNOSTIC", "PRACTO", "DENTAL", "DENTIST"], "any", "debit", C.EXPENSE, S.HEALTH, ""),
    (["LIC ", "INSURANCE", "HDFC ERGO", "ICICI LOMBARD", "STAR HEALTH", "NIVA BUPA", "POLICYBAZAAR"], "any", "debit", C.EXPENSE, S.INSURANCE, ""),
    (["BOOKMYSHOW", "PVR", "INOX", "DISTRICT"], "any", "debit", C.EXPENSE, S.ENTERTAINMENT, ""),
    (["INCOME TAX", "ADVANCE TAX", "TDS ", "GST"], "any", "debit", C.EXPENSE, S.TAXES, ""),
]


def ensure_builtin_rules():
    """Create built-in rules, adding any new ones shipped since the database was created."""
    existing = set(CategoryRule.objects.filter(is_builtin=True)
                   .values_list("keyword", "applies_to", "direction", "category"))
    wanted = set()
    for words, applies, direction, cat, spend, target in BUILTIN:
        for w in words:
            wanted.add((w, applies, direction, cat))
            if (w, applies, direction, cat) not in existing:
                CategoryRule.objects.create(keyword=w, applies_to=applies, direction=direction, category=cat,
                                            spend_category=spend, target=target, is_builtin=True)
    stale = existing - wanted
    for kw, applies, direction, cat in stale:
        CategoryRule.objects.filter(is_builtin=True, keyword=kw, applies_to=applies, direction=direction,
                                    category=cat).delete()


def ordered_rules():
    ensure_builtin_rules()
    rules = list(CategoryRule.objects.select_related("target_account"))
    user = sorted((r for r in rules if not r.is_builtin), key=lambda r: -r.id)
    builtin = sorted((r for r in rules if r.is_builtin), key=lambda r: r.id)
    return user + builtin


def resolve_target(target, owner):
    kinds = {T.ZERODHA: Account.Kind.BROKER, T.MF: Account.Kind.MF, T.FD: Account.Kind.FD}
    if target not in kinds:
        return None
    qs = Account.objects.filter(owner=owner, kind=kinds[target], is_active=True)
    return qs.filter(institution__iexact="Zerodha").first() if target == T.ZERODHA and qs.filter(
        institution__iexact="Zerodha").exists() else qs.first()


def match_rule(rules, description, amount, account):
    text = f" {description.upper()} "
    side = "card" if account.kind == Account.Kind.CARD else "bank"
    direction = "credit" if amount > 0 else "debit"
    for r in rules:
        if r.applies_to not in ("any", side) or r.direction not in ("any", direction):
            continue
        if r.keyword.upper() in text:
            return r
    return None


def apply_rule(txn, rule):
    txn.category = rule.category
    txn.spend_category = rule.spend_category
    txn.investment_account = rule.target_account or resolve_target(rule.target, txn.account.owner)
    txn.party = rule.party or txn.party
    txn.source = "rule"
    txn.rule = rule


def family_names():
    return [p.name.upper() for p in Person.objects.people()]


STOPWORDS = {"UPI", "DR", "CR", "NEFT", "IMPS", "RTGS", "POS", "ECOM", "TO", "BY", "FROM", "TRANSFER", "PAYMENT",
             "MB", "IB", "ACH", "NACH", "INB", "BIL", "ONL", "THE", "LTD", "PVT", "LIMITED", "PRIVATE", "INDIA",
             "BANK", "YESB", "HDFC", "ICIC", "SBIN", "UTIB", "KKBK", "PAYTM", "RAZORPAY", "BILLDESK", "PAYU", "VPA",
             "SENT", "RECEIVED", "PAY", "TXN", "REF", "NO", "MUMBAI", "BANGALORE", "BENGALURU", "DELHI", "IN"}


def keyword_guess(description):
    """A sensible default keyword for a 'remember this' rule, e.g. 'UPI/123/SWIGGY/YESB' -> 'SWIGGY'."""
    tokens = [t for t in re.split(r"[^A-Za-z0-9.&]+", description.upper()) if t]
    good = [t for t in tokens if len(t) >= 4 and not any(c.isdigit() for c in t) and t not in STOPWORDS]
    return good[0] if good else description[:30].upper()
