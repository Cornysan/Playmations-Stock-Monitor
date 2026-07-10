"""Strategy plugin registry.

Each module in this package is one swappable signal strategy and defines:

  NAME         unique id (used in DB, API and URLs)
  LABEL        display name for the UI
  DESCRIPTION  one or two sentences shown in the UI
  PARAMS       {key: {"default", "min", "max", "step", "label"}} — rendered as
               number inputs by the frontend; values are clamped server-side
  decide(closes, holding, params, macro_score=None) -> card dict

decide() evaluates the LAST bar of the series (point-in-time contract, exactly
like score.decide). The daily worker run calls it once with the real watchlist
holding; the backtester iterates it over history with the simulated position —
both therefore share the same logic and no look-ahead is possible.

Card contract (consumed by db.write_analysis, backtest.py and the web UI):
  signal       "BUY" | "SELL" | "HOLD"   (required — drives markers + badges)
  action / rationale / framing           free text for the banner
  flags / indicators                     dicts, persisted as JSON
  trend_score / momentum_score / macro_score / pillar_total   optional ints
"""
from __future__ import annotations
import importlib
import pkgutil

SIGNALS = ("BUY", "SELL", "HOLD")
DEFAULT = "three_pillars"

_registry: dict = {}


def _load() -> dict:
    if not _registry:
        for info in pkgutil.iter_modules(__path__):
            if info.name.startswith("_"):
                continue
            mod = importlib.import_module(f"{__name__}.{info.name}")
            if callable(getattr(mod, "decide", None)) and getattr(mod, "NAME", None):
                _registry[mod.NAME] = mod
    return _registry


def get(name: str):
    return _load().get(name)


def list_all() -> list[dict]:
    mods = sorted(_load().values(), key=lambda m: (m.NAME != DEFAULT, m.NAME))
    return [
        {
            "name": m.NAME,
            "label": getattr(m, "LABEL", m.NAME),
            "description": getattr(m, "DESCRIPTION", ""),
            "params": getattr(m, "PARAMS", {}),
        }
        for m in mods
    ]


def resolve_params(mod, params: dict | None) -> dict:
    """Defaults overlaid with `params`; unknown keys dropped, values clamped."""
    out = {}
    for key, spec in getattr(mod, "PARAMS", {}).items():
        value = (params or {}).get(key, spec["default"])
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = spec["default"]
        value = max(spec.get("min", value), min(spec.get("max", value), value))
        out[key] = int(value) if isinstance(spec["default"], int) else value
    return out


def run(mod, closes: list[float], holding: bool, params: dict | None = None,
        macro_score: int | None = None) -> dict:
    """decide() plus contract validation/normalisation."""
    card = mod.decide(closes, bool(holding), resolve_params(mod, params), macro_score)
    if card.get("signal") not in SIGNALS:
        raise ValueError(f"{mod.NAME}: invalid signal {card.get('signal')!r}")
    card.setdefault("action", card["signal"])
    card.setdefault("rationale", "")
    card.setdefault("framing", "")
    card.setdefault("flags", {})
    card.setdefault("indicators", {})
    for key in ("trend_score", "momentum_score", "macro_score", "pillar_total"):
        card.setdefault(key, None)
    return card
