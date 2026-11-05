"""The family's own goals, beliefs and rules, which every AI review and Ask question works from.

Questions are defined here and answers stored as JSON on Household, so changing the form
needs no migration. Blank answers are passed on as unanswered: Claude asks or states an
assumption instead of deciding for you.
"""
from django import forms

from core.models import AllocationTarget, Household

CONTROL = {"class": "form-control form-control-sm"}
SELECT = {"class": "form-select form-select-sm"}
EXPENSE_ROWS = 6


def _q(key, label, kind="text", choices=None, help_text=""):
    return {"key": key, "label": label, "kind": kind, "choices": choices or [], "help": help_text}


STABILITY = ["Very stable", "Fairly stable", "Could change", "At real risk"]

SECTIONS = [
    {"title": "Your goals", "questions": [
        _q("fi_meaning", "What does financial independence mean for you?", "choice",
           ["Stop working altogether", "Work only by choice", "Move to lower-paid, meaningful work", "One of us stops, the other continues"]),
        _q("fi_age", "At what age, for each of you?", help_text="For example: Saurabh 50, Pritika 48."),
        _q("spend_at_fi", "Monthly spending you'd want at FI, in today's rupees", "money",
           help_text="Without the home loan EMI."),
        _q("why", "What is the money for?", "long", help_text="What you most want it to make possible."),
    ]},
    {"title": "Risk", "questions": [
        _q("max_fall", "How big a fall in your portfolio could you live with without selling?", "choice",
           ["10%", "20%", "30%", "40%", "50% or more"]),
        _q("last_fall", "What did you actually do in the last big market fall?", "choice",
           ["Bought more", "Did nothing", "Sold some", "Sold a lot", "Wasn't invested then"]),
        _q("income_saurabh", "How stable is Saurabh's income?", "choice", STABILITY),
        _q("income_pritika", "How stable is Pritika's income?", "choice", STABILITY),
        _q("max_equity", "The most you'd ever hold in equity, as a %"),
    ]},
    {"title": "Family and safety", "questions": [
        _q("children", "Children's ages and plans", "long", help_text="School, college, abroad or India."),
        _q("parents", "Support for parents, now or expected", "long"),
        _q("life_cover", "Term life cover, for each of you"),
        _q("health_cover", "Health cover outside your employers"),
        _q("tax_regime", "Income tax regime", "choice", ["New regime", "Old regime", "One each"]),
    ]},
    {"title": "Investing rules", "questions": [
        _q("style", "Active funds and stocks, or passive index funds?", "choice",
           ["Passive only", "Mostly passive", "A mix", "Mostly active"]),
        _q("advisors", "How do you feel about Weekend Investing, smallcases and ET Money?", "long"),
        _q("gold", "Your view on gold", "long", help_text="How much you want and why."),
        _q("loan_vs_invest", "Spare money: prepay the home loan or invest?", "choice",
           ["Prepay first", "Mostly prepay", "Split it", "Mostly invest", "Invest first"]),
        _q("property", "Would you buy more property, or sell any?", "long"),
        _q("never", "Anything Claude must never touch or suggest", "long",
           help_text="For example: never sell the SGBs. These are treated as firm rules."),
    ]},
    {"title": "How Claude should advise you", "questions": [
        _q("boldness", "How should it advise?", "choice", ["Cautious", "Balanced", "Bold"]),
        _q("change_pace", "How often are you happy to make changes?", "choice",
           ["Only when essential", "Once or twice a year", "Every quarter", "Monthly"]),
        _q("stop_raising", "Topics to stop raising", "long"),
    ]},
]
QUESTIONS = [q for s in SECTIONS for q in s["questions"]]


def _field(q):
    common = {"label": q["label"], "required": False, "help_text": q["help"]}
    if q["kind"] == "choice":
        return forms.ChoiceField(choices=[("", "Not answered")] + [(c, c) for c in q["choices"]],
                                 widget=forms.Select(attrs=SELECT), **common)
    if q["kind"] == "money":
        return forms.DecimalField(max_digits=14, decimal_places=0, min_value=0,
                                  widget=forms.NumberInput(attrs=CONTROL), **common)
    if q["kind"] == "long":
        return forms.CharField(max_length=2000, widget=forms.Textarea(attrs={**CONTROL, "rows": 2}), **common)
    return forms.CharField(max_length=300, widget=forms.TextInput(attrs=CONTROL), **common)


class GoalsForm(forms.Form):
    note = forms.CharField(label="My note to Claude", required=False, max_length=8000,
                           widget=forms.Textarea(attrs={**CONTROL, "rows": 9}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for q in QUESTIONS:
            self.fields[q["key"]] = _field(q)
        for i in range(EXPENSE_ROWS):
            self.fields[f"exp_what_{i}"] = forms.CharField(required=False, max_length=120, widget=forms.TextInput(attrs=CONTROL))
            self.fields[f"exp_year_{i}"] = forms.IntegerField(required=False, min_value=2000, max_value=2100,
                                                              widget=forms.NumberInput(attrs=CONTROL))
            self.fields[f"exp_amount_{i}"] = forms.DecimalField(required=False, max_digits=14, decimal_places=0, min_value=0,
                                                                widget=forms.NumberInput(attrs=CONTROL))

    def sections(self):
        return [{"title": s["title"], "fields": [self[q["key"]] for q in s["questions"]]} for s in SECTIONS]

    def expense_rows(self):
        return [(self[f"exp_what_{i}"], self[f"exp_year_{i}"], self[f"exp_amount_{i}"]) for i in range(EXPENSE_ROWS)]


def initial_values(household=None):
    hh = household or Household.get()
    values = {"note": hh.note_to_claude, **{k: v for k, v in hh.goals.items() if k != "big_expenses"}}
    for i, row in enumerate(hh.goals.get("big_expenses", [])[:EXPENSE_ROWS]):
        values |= {f"exp_what_{i}": row.get("what"), f"exp_year_{i}": row.get("year"), f"exp_amount_{i}": row.get("amount")}
    return values


def save(data):
    hh = Household.get()
    hh.note_to_claude = data["note"].strip()
    goals = {}
    for q in QUESTIONS:
        value = data.get(q["key"])
        if value not in (None, ""):
            goals[q["key"]] = int(value) if q["kind"] == "money" else value.strip()
    rows = [{"what": data[f"exp_what_{i}"].strip(), "year": data[f"exp_year_{i}"],
             "amount": int(data[f"exp_amount_{i}"]) if data[f"exp_amount_{i}"] is not None else None}
            for i in range(EXPENSE_ROWS) if data[f"exp_what_{i}"].strip()]
    if rows:
        goals["big_expenses"] = rows
    hh.goals = goals
    hh.save(update_fields=["note_to_claude", "goals"])


def plan_gaps(household=None):
    """Numbers on the Plan tab that Claude would otherwise have to choose for you."""
    hh = household or Household.get()
    gaps = []
    if not hh.target_fi_date:
        gaps.append("Target FI date")
    if not AllocationTarget.objects.exists():
        gaps.append("Target allocation")
    return gaps


def for_claude(household=None):
    hh = household or Household.get()
    answers = {q["label"]: hh.goals[q["key"]] for q in QUESTIONS if q["key"] in hh.goals}
    return {
        "note_to_claude": hh.note_to_claude or None,
        "answers": answers,
        "big_future_expenses": hh.goals.get("big_expenses", []),
        "not_answered_yet": [q["label"] for q in QUESTIONS if q["key"] not in hh.goals],
        "plan_numbers_left_blank": plan_gaps(hh),
    }
