"""JSON schema for review output (enforced via structured outputs)."""

VERDICTS = ["exit", "reduce", "hold", "add", "action"]

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence: the single most important thing right now."},
        "summary": {"type": "string", "description": "3-6 sentences on where things stand (markdown allowed)."},
        "fi_progress": {"type": "string", "description": "Progress to crossover, what moved it, and whether the assumptions look sane."},
        "recommendations": {
            "type": "array",
            "description": "Prioritised actions, most important first. Empty if nothing needs doing.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "verdict": {"type": "string", "enum": VERDICTS},
                    "instrument_id": {"type": "integer", "description": "Holding id from the payload, or 0"},
                    "amount": {"type": "number", "description": "Rupees involved, or 0"},
                    "urgency": {"type": "string", "enum": ["high", "normal", "low"]},
                    "rationale": {"type": "string"},
                },
                "required": ["title", "verdict", "instrument_id", "amount", "urgency", "rationale"],
                "additionalProperties": False,
            },
        },
        "holding_verdicts": {
            "type": "array",
            "description": "One entry per self-managed holding (monthly review). May be empty for weekly checks.",
            "items": {
                "type": "object",
                "properties": {
                    "instrument_id": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["exit", "reduce", "hold", "add"]},
                    "amount": {"type": "number", "description": "Rupees to sell/buy, or 0 for hold"},
                    "rationale": {"type": "string"},
                },
                "required": ["instrument_id", "verdict", "amount", "rationale"],
                "additionalProperties": False,
            },
        },
        "advisor_strategy_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "advisor": {"type": "string"},
                    "assessment": {"type": "string"},
                    "flag": {"type": "boolean", "description": "True if exposure should be reconsidered"},
                },
                "required": ["advisor", "assessment", "flag"],
                "additionalProperties": False,
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
        "questions_for_you": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "summary", "fi_progress", "recommendations", "holding_verdicts",
                 "advisor_strategy_notes", "risks", "data_gaps", "questions_for_you"],
    "additionalProperties": False,
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Markdown answer to the question"},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "data_gaps"],
    "additionalProperties": False,
}

CADENCE_INSTRUCTIONS = {
    "weekly": (
        "This is the WEEKLY light check. Keep it short. Only raise things that can't wait for the month-end review: "
        "idle cash well above the emergency fund, allocation drift outside its band, stale data (Zerodha not pulled, "
        "no CAS recently), a self-managed holding that fell more than 15% in a month, anything urgent in the loan or "
        "FI picture. holding_verdicts should normally be empty. If nothing needs doing, say so in the headline."
    ),
    "monthly": (
        "This is the MONTHLY full review. Cover: progress to crossover and what moved it (savings vs market); "
        "where this month's new money should go given the target allocation; any rebalancing across asset classes; "
        "an explicit exit/reduce/hold/add verdict for EVERY self-managed holding in holding_verdicts; a strategy-level "
        "note on each advisor group; tax-aware ideas for this financial year; and the data gaps that most limit the "
        "advice. If no target allocation is set, propose one suited to their FI timeline as a recommendation."
    ),
}
