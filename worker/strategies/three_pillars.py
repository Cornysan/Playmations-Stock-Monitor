"""Default strategy: thin wrapper around the immutable score.py cascade.

score.py stays untouched — this module only maps its rich decision card onto
the generic strategy contract. The action→signal mapping mirrors what the UI
always displayed (RE-ENTRY/TACTICAL → BUY, EXIT/STAY OUT → SELL, rest → HOLD).
"""
from __future__ import annotations

import score

NAME = "three_pillars"
LABEL = "Three Pillars (Standard)"
DESCRIPTION = ("Trend/Momentum/Macro-Säulen mit Exhaustion-/Rebound-Kaskade "
               "aus score.py — die bisherige Standard-Logik.")
PARAMS = {
    "slope_lookback": {"default": 5, "min": 2, "max": 20, "step": 1,
                       "label": "Slope-Lookback (Bars)"},
}


def _signal(action: str) -> str:
    if action.startswith(("RE-ENTRY", "TACTICAL")):
        return "BUY"
    if action.startswith(("EXIT", "STAY OUT")):
        return "SELL"
    return "HOLD"


def decide(closes: list[float], holding: bool, params: dict,
           macro_score: int | None = None) -> dict:
    card = score.score_symbol(closes, macro_score=macro_score, holding=holding,
                              slope_lookback=params["slope_lookback"])
    pillars, decision = card["pillars"], card["decision"]
    return {
        "signal": _signal(decision["action"]),
        "action": decision["action"],
        "rationale": decision["rationale"],
        "framing": decision["framing"],
        "flags": {
            **decision["flags"],
            "trend_detail": pillars["trend"]["detail"],
            "momentum_detail": pillars["momentum"]["detail"],
            "warning": card.get("warning"),
        },
        "indicators": card["indicators"],
        "trend_score": pillars["trend"]["score"],
        "momentum_score": pillars["momentum"]["score"],
        "macro_score": pillars["macro_sentiment"]["score"],
        "pillar_total": card["pillar_total"],
    }
