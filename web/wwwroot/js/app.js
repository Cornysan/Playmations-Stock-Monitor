// Shared helpers — pure presentation, no signal math happens in the frontend.

// Kurzlabel fürs UI: alles auf BUY / HOLD / SELL eingedampft. Die
// Original-Action aus score.py bleibt unverändert in DB/API/Tooltip.
function actionLabel(action) {
  if (!action) return null;
  if (action.startsWith("RE-ENTRY")) return "BUY";
  if (action.startsWith("TACTICAL")) return "BUY";      // Gegentrend-Rebound
  if (action.startsWith("EXIT")) return "SELL";
  if (action.startsWith("STAY OUT")) return "SELL";     // meiden
  return "HOLD"; // HOLD (ride/under review), WAIT, OBSERVE
}

// Badge-/Banner-Farbe: BUY grün, HOLD grau, SELL rot.
function actionClass(action) {
  const label = actionLabel(action);
  if (!label) return "act-none";
  return { BUY: "act-buy", HOLD: "act-hold", SELL: "act-sell" }[label];
}

function fmtPrice(v) {
  if (v == null) return "—";
  const abs = Math.abs(v);
  const digits = abs >= 100 ? 2 : abs >= 1 ? 2 : 4;
  return v.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtSigned(v) {
  if (v == null) return "?";
  return (v > 0 ? "+" : "") + v;
}

function signClass(v) {
  if (v == null) return "zero";
  return v > 0 ? "pos" : v < 0 ? "neg" : "zero";
}

// Top flags for the badge tooltip (2–3 strongest, see plan §8.1)
function flagTooltip(flags) {
  if (!flags) return "";
  const all = [
    ...(flags.bearish || []).map((f) => "▼ " + f),
    ...(flags.exhaustion || []).map((f) => "⚠ " + f),
    ...(flags.rebound || []).map((f) => "▲ " + f),
  ];
  if (flags.death_cross) all.unshift("✝ death cross active");
  return all.slice(0, 3).join("\n");
}

function fmtAsOf(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
