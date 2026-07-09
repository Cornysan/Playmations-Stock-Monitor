// Shared helpers — pure presentation, no signal math happens in the frontend.

// Action → badge class (mapping from PROJEKTPLAN §8.1)
function actionClass(action) {
  if (!action) return "act-none";
  if (action.startsWith("RE-ENTRY")) return "act-strong-buy";
  if (action.startsWith("HOLD (ride")) return "act-buy";
  if (action.startsWith("TACTICAL")) return "act-tactical";
  if (action.startsWith("WAIT") || action.startsWith("HOLD (under")) return "act-wait";
  if (action.startsWith("EXIT")) return "act-exit";
  if (action.startsWith("STAY OUT")) return "act-avoid";
  return "act-observe"; // OBSERVE / HOLD / OBSERVE
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
