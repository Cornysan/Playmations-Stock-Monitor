// Shared helpers — pure presentation, no signal math happens in the frontend.

// Kurzlabel fürs UI: alles auf BUY / HOLD / SELL eingedampft. Neue Analysen
// tragen das normalisierte `signal` direkt (kommt aus der Strategie); das
// Action-Mapping bleibt als Fallback für Bestandsdaten aus score.py-Zeiten.
function actionLabel(action, signal) {
  if (signal === "BUY" || signal === "SELL" || signal === "HOLD") return signal;
  if (!action) return null;
  if (action.startsWith("RE-ENTRY")) return "BUY";
  if (action.startsWith("TACTICAL")) return "BUY";      // Gegentrend-Rebound
  if (action.startsWith("EXIT")) return "SELL";
  if (action.startsWith("STAY OUT")) return "SELL";     // meiden
  return "HOLD"; // HOLD (ride/under review), WAIT, OBSERVE
}

// Badge-/Banner-Farbe: BUY grün, HOLD weiß, SELL rot.
function actionClass(action, signal) {
  const label = actionLabel(action, signal);
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

function fmtPct(v, digits = 2) {
  if (v == null) return "—";
  return (v > 0 ? "+" : "") + v.toFixed(digits) + "%";
}

function fmtUsd(v) {
  if (v == null) return "—";
  return v.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " $";
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

// ---------------------------------------------------------------------------
// Geteilter Detail-Zustand (Chart + Analyse) — genutzt vom rechten Panel der
// Startseite (Desktop) und von symbol.html (Mobile / Deep-Link).
// ---------------------------------------------------------------------------

function detailState() {
  return {
    symbol: null,
    analysis: null,
    analysisMissing: false,
    lastClose: null,
    range: "1y",
    chart: null,
    series: null,
    markersApi: null,
    firstBarTime: null,
    strategies: null,     // [{name,label,description,params,saved_params,active}]
    stratName: null,
    stratParams: {},
    backtest: null,
    backtestError: null,
    btLoading: false,
    open: { pillars: false, flags: false, ind: false, macro: false, bt: false },

    async show(symbol) {
      if (!symbol) return;
      this.symbol = symbol;
      this.analysis = null;
      this.analysisMissing = false;
      this.lastClose = null;
      this.backtest = null;
      this.backtestError = null;
      this.applyMarkers();
      await Promise.all([this.loadAnalysis(), this.loadChart(), this.ensureStrategies()]);
      await this.loadBacktest();
    },

    async loadAnalysis() {
      const resp = await fetch("/api/symbols/" + encodeURIComponent(this.symbol) + "/analysis");
      if (resp.ok) this.analysis = await resp.json();
      else this.analysisMissing = true;
    },

    async loadChart() {
      const resp = await fetch("/api/symbols/" + encodeURIComponent(this.symbol) + "/bars?range=" + this.range);
      if (!resp.ok) { this.clearChart(); return; }
      const data = await resp.json();
      this.lastClose = data.bars.length ? data.bars[data.bars.length - 1].close : null;
      this.renderChart(data);
    },

    clearChart() {
      if (!this.series) return;
      this.series.candles.setData([]);
      this.series.volume.setData([]);
      this.series.ema20.setData([]);
      this.series.ema50.setData([]);
      this.series.ema200.setData([]);
      this.firstBarTime = null;
      this.applyMarkers();
    },

    renderChart(data) {
      const el = document.getElementById("chart");
      if (!el) return;
      // Alpine blendet den Container erst im nächsten Frame ein — ein Chart,
      // das bei 0px Größe erzeugt wird, bleibt sonst auf der Default-Canvas.
      if (!this.chart && el.clientWidth === 0) {
        requestAnimationFrame(() => this.renderChart(data));
        return;
      }
      if (!this.chart) {
        const LWC = LightweightCharts;
        this.chart = LWC.createChart(el, {
          autoSize: true,
          layout: { background: { color: "transparent" }, textColor: "#8b949e", attributionLogo: false },
          grid: { vertLines: { color: "#1a2029" }, horzLines: { color: "#1a2029" } },
          crosshair: { mode: LWC.CrosshairMode.Normal },
          rightPriceScale: { borderColor: "#21262d" },
          timeScale: { borderColor: "#21262d" },
        });
        this.series = {
          candles: this.chart.addSeries(LWC.CandlestickSeries, {
            upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
            wickUpColor: "#26a69a", wickDownColor: "#ef5350",
          }),
          volume: this.chart.addSeries(LWC.HistogramSeries, {
            priceScaleId: "volume", priceFormat: { type: "volume" },
            color: "rgba(139,148,158,.25)", lastValueVisible: false, priceLineVisible: false,
          }),
          ema20: this.chart.addSeries(LWC.LineSeries, { color: "#e3b341", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }),
          ema50: this.chart.addSeries(LWC.LineSeries, { color: "#39c5cf", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }),
          ema200: this.chart.addSeries(LWC.LineSeries, { color: "#a371f7", lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }),
        };
        this.chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
        this.markersApi = LWC.createSeriesMarkers(this.series.candles, []);
      }
      this.series.candles.setData(data.bars);
      this.series.volume.setData(data.bars.map((b) => ({
        time: b.time, value: b.volume ?? 0,
        color: b.close >= b.open ? "rgba(38,166,154,.25)" : "rgba(239,83,80,.25)",
      })));
      this.series.ema20.setData(data.ema20);
      this.series.ema50.setData(data.ema50);
      this.series.ema200.setData(data.ema200);
      this.firstBarTime = data.bars.length ? data.bars[0].time : null;
      this.applyMarkers();
      this.chart.timeScale().fitContent();
    },

    // ---- Strategien + Backtest --------------------------------------------

    async ensureStrategies() {
      if (this.strategies) return;
      const resp = await fetch("/api/strategies");
      if (!resp.ok) { this.strategies = []; return; }
      this.strategies = await resp.json();
      const active = this.strategies.find((s) => s.active) || this.strategies[0];
      if (active) this.selectStrategy(active.name, false);
    },

    currentStrategy() {
      return (this.strategies || []).find((s) => s.name === this.stratName) || null;
    },

    selectStrategy(name, reload = true) {
      this.stratName = name;
      const s = this.currentStrategy();
      this.stratParams = {};
      for (const [k, spec] of Object.entries(s?.params || {}))
        this.stratParams[k] = s.saved_params?.[k] ?? spec.default;
      if (reload) this.loadBacktest();
    },

    async loadBacktest() {
      if (!this.symbol || !this.stratName) return;
      const requested = this.symbol;
      this.btLoading = true;
      this.backtestError = null;
      const url = "/api/symbols/" + encodeURIComponent(requested) + "/backtest"
        + "?strategy=" + encodeURIComponent(this.stratName)
        + "&params=" + encodeURIComponent(JSON.stringify(this.stratParams));
      const resp = await fetch(url);
      if (requested !== this.symbol) return; // Nutzer hat inzwischen gewechselt
      this.btLoading = false;
      if (!resp.ok) {
        this.backtest = null;
        this.backtestError = (await resp.json().catch(() => ({}))).error || "Backtest fehlgeschlagen";
        this.applyMarkers();
        return;
      }
      this.backtest = await resp.json();
      this.applyMarkers();
    },

    applyMarkers() {
      if (!this.markersApi) return;
      const first = this.firstBarTime;
      const sigs = (this.backtest?.signals || []).filter((s) => !first || s.date >= first);
      this.markersApi.setMarkers(sigs.map((s) => ({
        time: s.date,
        position: s.type === "buy" ? "belowBar" : "aboveBar",
        color: s.type === "buy" ? "#26a69a" : "#ef5350",
        shape: s.type === "buy" ? "arrowUp" : "arrowDown",
        // pending = Signal vom letzten Close, Ausführung steht noch aus
        text: (s.type === "buy" ? "Buy" : "Sell") + (s.pending ? "*" : ""),
      })));
    },

    btNote() {
      const m = this.backtest?.meta || {};
      const exec = m.execution === "close"
        ? "Ausführung zum Signal-Close" : "Ausführung zur nächsten Eröffnung (wie Live-Trading)";
      return "Zeitraum " + (m.from || "?") + " – " + (m.to || "?") + " · " + exec
        + " · ohne Gebühren/Slippage · Macro-Säule im Backtest nicht verfügbar"
        + " · * = Signal wartet auf Ausführung";
    },

    async saveStrategy(setActive = false) {
      const body = { params: this.stratParams };
      if (setActive) body.active = true;
      const resp = await fetch("/api/strategies/" + encodeURIComponent(this.stratName), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        alert(resp.status === 401 ? "Nur mit Admin-Login möglich." : "Speichern fehlgeschlagen.");
        return;
      }
      const s = this.currentStrategy();
      if (s) {
        s.saved_params = { ...this.stratParams };
        if (setActive) this.strategies.forEach((x) => { x.active = x.name === s.name; });
      }
    },

    btSummary() {
      if (this.backtestError) return this.backtestError;
      const st = this.backtest?.stats;
      if (!st) return this.btLoading ? "läuft…" : "";
      const parts = [fmtPct(st.total_return_pct), st.n_trades + " Trades"];
      if (st.win_rate_pct != null) parts.push(st.win_rate_pct.toFixed(0) + "% Win");
      return parts.join(" · ");
    },

    btRows() {
      const st = this.backtest?.stats;
      if (!st) return [];
      return [
        ["Rendite", fmtPct(st.total_return_pct), signClass(st.total_return_pct)],
        ["Buy & Hold", fmtPct(st.buy_hold_return_pct), signClass(st.buy_hold_return_pct)],
        ["Trades", String(st.n_trades), ""],
        ["Win-Rate", st.win_rate_pct != null ? st.win_rate_pct.toFixed(0) + "%" : "—", ""],
        ["Ø Gewinn", fmtPct(st.avg_win_pct), "pos"],
        ["Ø Verlust", fmtPct(st.avg_loss_pct), "neg"],
        ["Profit-Faktor", st.profit_factor != null ? String(st.profit_factor) : "—", ""],
        ["Max Drawdown", st.max_drawdown_pct != null ? "−" + st.max_drawdown_pct + "%" : "—", "neg"],
        ["Exposure", st.exposure_pct != null ? st.exposure_pct.toFixed(0) + "%" : "—", ""],
      ];
    },

    async toggleHolding() {
      const target = !this.analysis.holding;
      const resp = await fetch("/api/watchlist/" + encodeURIComponent(this.symbol), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ holding: target }),
      });
      // Framing text still stems from the last run; the badge flips on the next worker run.
      if (resp.ok) this.analysis.holding = target ? 1 : 0;
    },

    async toggleAutotrade() {
      const target = !this.analysis.autotrade;
      if (target && !confirm(this.symbol + " automatisch handeln (Paper-Account)? "
          + "Der Worker kauft/verkauft dann eigenständig nach Signal.")) return;
      const resp = await fetch("/api/watchlist/" + encodeURIComponent(this.symbol), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ autotrade: target }),
      });
      if (resp.ok) this.analysis.autotrade = target ? 1 : 0;
    },

    flagSummary() {
      const f = this.analysis?.flags || {};
      const parts = [];
      if ((f.exhaustion || []).length) parts.push((f.exhaustion || []).length + " exhaustion");
      if ((f.bearish || []).length) parts.push((f.bearish || []).length + " bearish");
      if ((f.rebound || []).length) parts.push((f.rebound || []).length + " rebound");
      if (f.death_cross) parts.push("death cross");
      return parts.join(" · ") || "keine aktiven Flags";
    },

    indicatorRows() {
      const ind = this.analysis?.indicators || {};
      const skip = new Set(["warning"]);
      return Object.entries(ind).filter(([k]) => !skip.has(k))
        .map(([k, v]) => [k, v == null ? "—" : typeof v === "number" ? v.toLocaleString("de-DE", { maximumFractionDigits: 4 }) : String(v)]);
    },
  };
}
