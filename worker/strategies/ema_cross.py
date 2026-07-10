"""Example strategy: EMA crossover regime (golden/death cross).

Deliberately simple — demonstrates the params UI and serves as the template
for writing new strategies. Reuses indicators.ema_series (never re-implement
the numerics).
"""
from __future__ import annotations

import indicators as I

NAME = "ema_cross"
LABEL = "EMA-Crossover"
DESCRIPTION = ("Long, solange die schnelle EMA über der langsamen liegt "
               "(Golden-Cross-Regime); Ausstieg beim Death-Cross. "
               "Vorlage für eigene Strategien.")
PARAMS = {
    "fast": {"default": 20, "min": 5, "max": 100, "step": 1, "label": "Schnelle EMA"},
    "slow": {"default": 50, "min": 10, "max": 250, "step": 1, "label": "Langsame EMA"},
}


def decide(closes: list[float], holding: bool, params: dict,
           macro_score: int | None = None) -> dict:
    fast, slow = params["fast"], params["slow"]
    if fast >= slow:  # nonsensical input from the UI — keep it well-defined
        fast = max(PARAMS["fast"]["min"], slow - 1)

    ema_fast = I.ema_series(closes, fast)
    ema_slow = I.ema_series(closes, slow)
    f, s = ema_fast[-1], ema_slow[-1]
    if f is None or s is None:
        return {
            "signal": "HOLD",
            "action": "OBSERVE",
            "rationale": f"Zu wenig Historie für EMA{slow} ({len(closes)} Bars).",
            "framing": "Warten, bis genug Bars für beide EMAs vorliegen.",
            "flags": {"warning": f"nur {len(closes)} Bars"},
            "indicators": {"n_bars": len(closes), "close": closes[-1]},
        }

    f_prev, s_prev = ema_fast[-2], ema_slow[-2]
    fresh = f_prev is not None and s_prev is not None and (f_prev <= s_prev) != (f <= s)
    spread_pct = round((f / s - 1) * 100, 2)
    bullish = f > s

    if bullish:
        signal = "BUY"
        action = "BUY (frischer Golden Cross)" if fresh else "BUY (Golden-Cross-Regime)"
        rationale = f"EMA{fast} über EMA{slow} ({spread_pct:+.2f}%)."
        framing = ("Long bleiben, solange die schnelle EMA über der langsamen liegt."
                   if holding else "Bullisches Regime — Einstieg zum nächsten Signal-Close.")
        flags = {"rebound": [f"EMA{fast}>EMA{slow} ({spread_pct:+.2f}%)"]
                 + (["frischer Golden Cross auf diesem Bar"] if fresh else [])}
    else:
        signal = "SELL"
        action = "SELL (frischer Death Cross)" if fresh else "SELL (Death-Cross-Regime)"
        rationale = f"EMA{fast} unter EMA{slow} ({spread_pct:+.2f}%)."
        framing = ("Position schließen — bärisches Regime."
                   if holding else "Draußen bleiben, bis die schnelle EMA zurückkreuzt.")
        flags = {"bearish": [f"EMA{fast}<EMA{slow} ({spread_pct:+.2f}%)"]
                 + (["frischer Death Cross auf diesem Bar"] if fresh else [])}

    return {
        "signal": signal,
        "action": action,
        "rationale": rationale,
        "framing": framing,
        "flags": flags,
        "indicators": {
            "n_bars": len(closes),
            "close": closes[-1],
            f"ema{fast}": round(f, 4),
            f"ema{slow}": round(s, 4),
            "spread_pct": spread_pct,
        },
    }
