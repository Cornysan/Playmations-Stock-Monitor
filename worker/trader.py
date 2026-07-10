#!/usr/bin/env python3
"""
trader.py
=========
Auto-Trading-Executor (Pott-Modell): Kapital wird gleichmäßig auf die
Auto-Trade-Symbole verteilt — Pott = Equity / N. Pro Symbol all-in/all-out,
gesteuert vom normalisierten Signal der aktiven Strategie (analysis.signal).

Ablauf pro Tagesrun (main.run_once):
  sync(con)          VOR der Analyse: Order-Status refreshen, watchlist.holding
                     aus den echten Alpaca-Positionen setzen, Konto-Snapshot.
  trade(con, as_of)  NACH der Analyse: BUY im Flat → Notional-Kauf (Pott),
                     SELL in Position → Market-Sell der ganzen Position.

Jedes Auto-Trade-Symbol handelt in seinem eingelockten Timeframe (watchlist.
strat_timeframe, beim Aktivieren im UI eingefroren); Symbole ohne Lock fallen
auf TRADING_TIMEFRAME zurück. Tagesrun → trade(timeframe="1d"), Stunden-Läufe
→ trade(timeframe="1h") mit stündlichen Idempotenz-Fenstern.

Sicherungen: TRADING_ENABLED-Opt-in, client_order_id-Idempotenz (pro Symbol,
Run-Fenster und Seite), MAX_ORDERS_PER_RUN-Circuit-Breaker, PDT-Schutz auf
Live-Konten < 25k USD, Fehler einzelner Orders brechen weder den Run noch die
restlichen Orders.
"""
from __future__ import annotations
import datetime as dt
import logging
import time

import broker
import config
import db

log = logging.getLogger("trader")

MIN_NOTIONAL = 1.0   # Alpaca-Minimum für Notional-Orders
FILL_POLL_TRIES = 5  # nach einem Buy mit Risk-Config: kurz auf den Fill warten,
FILL_POLL_PAUSE = 3  # um die Schutz-Order sofort zu platzieren (sonst übernimmt sync)


def enabled() -> bool:
    return config.TRADING_ENABLED and broker.configured()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _order_row(alpaca: dict | None, cid: str, symbol: str, side: str,
               notional: float | None = None, error: str | None = None) -> dict:
    a = alpaca or {}
    return {
        "client_order_id": cid,
        "alpaca_id": a.get("id"),
        "symbol": symbol,
        "side": side,
        "notional": notional,
        "submitted_at": a.get("submitted_at") or now_iso(),
        "status": "error" if error else a.get("status", "unknown"),
        "filled_qty": float(a["filled_qty"]) if a.get("filled_qty") else None,
        "filled_avg_price": float(a["filled_avg_price"]) if a.get("filled_avg_price") else None,
        "filled_at": a.get("filled_at"),
        "error": error,
    }


# ---------------------------------------------------------------------------
# Phase 1: sync (vor der Analyse)
# ---------------------------------------------------------------------------

def sync(con) -> None:
    # 1. Nicht-finale Orders gegen Alpaca refreshen (Fills der letzten Eröffnung)
    for row in db.open_orders(con):
        try:
            o = broker.get_order_by_client_id(row["client_order_id"])
        except broker.BrokerError as e:
            log.warning("order refresh %s failed: %s", row["client_order_id"], e)
            continue
        if o is not None:
            db.upsert_order(con, _order_row(
                o, row["client_order_id"], row["symbol"], row["side"],
                row["notional"]))

    # 2. holding der Auto-Trade-Symbole = echte Alpaca-Position (Quelle der Wahrheit).
    #    Der manuelle Toggle bleibt für alle übrigen Symbole maßgeblich.
    positions = broker.get_positions()
    for sym in db.autotrade_symbols(con):
        con.execute("UPDATE watchlist SET holding=? WHERE symbol=?",
                    (1 if sym in positions else 0, sym))
    con.commit()

    # 3. Konto-Snapshot für UI/Verlauf
    acct = broker.get_account()
    db.write_broker_snapshot(con, now_iso(), acct["equity"], acct["cash"],
                             acct["buying_power"], positions)
    log.info("broker sync: equity=%.2f cash=%.2f positions=%d",
             acct["equity"], acct["cash"], len(positions))

    # 4. Schutz-Pass: jede Auto-Trade-Position mit Risk-Config im Lock braucht
    #    eine offene Schutz-Order — heilt verpasste Fills, Neustarts und
    #    Bruchstück-Reste nach gefeuerten Stops selbstständig.
    risks = db.autotrade_risk(con)
    today = dt.datetime.now(dt.timezone.utc)
    key = today.strftime("%Y%m%dT%H%M")
    for sym, risk in risks.items():
        pos = positions.get(sym)
        if pos is None or db.open_protection_order(con, sym) is not None:
            continue
        if _pdt_blocked(con, sym, today.strftime("%Y%m%d"), acct["equity"]):
            log.warning("%s: Schutz-Order aufgeschoben (PDT-Schutz, Live < 25k)", sym)
            continue
        qty = int(pos["qty"])
        if qty >= 1:
            _place_protection(con, sym, qty, risk, pos["avg_entry_price"], key)
        else:
            # Bruchstück-Rest (z. B. Alt-Position nach gefeuertem Stop):
            # nicht stop-fähig → glattstellen
            cid = f"spm-{sym}-{key}-cleanup"
            try:
                o = broker.submit_market_sell(sym, pos["qty_raw"], cid)
                db.upsert_order(con, _order_row(o, cid, sym, "sell"))
                log.info("%s: Bruchstück-Rest (%s Stk.) glattgestellt", sym, pos["qty_raw"])
            except broker.BrokerError as e:
                log.error("%s: cleanup failed: %s", sym, e)


# ---------------------------------------------------------------------------
# Phase 2: trade (nach der Analyse)
# ---------------------------------------------------------------------------

def _place_protection(con, symbol: str, qty: int, risk: dict, entry_price: float,
                      key: str) -> None:
    """Schutz-Order beim Broker platzieren: Trailing-Stop wenn konfiguriert,
    sonst fester Stop-Loss. GTC — greift intra-Kerze, unabhängig vom Worker."""
    trail = risk.get("trail_pct")
    stop = risk.get("stop_loss_pct")
    try:
        if trail:
            cid = f"spm-{symbol}-{key}-trail"
            o = broker.submit_trailing_stop(symbol, qty, float(trail), cid)
            log.info("%s: trailing stop %.2f%% für %d Stk. platziert (%s)",
                     symbol, float(trail), qty, o.get("status"))
        elif stop:
            cid = f"spm-{symbol}-{key}-stopl"
            stop_price = entry_price * (1 - float(stop) / 100.0)
            o = broker.submit_stop_sell(symbol, qty, stop_price, cid)
            log.info("%s: stop-loss @ %.2f für %d Stk. platziert (%s)",
                     symbol, stop_price, qty, o.get("status"))
        else:
            return
        db.upsert_order(con, _order_row(o, cid, symbol, "sell"))
    except broker.BrokerError as e:
        log.error("%s: Schutz-Order fehlgeschlagen (sync versucht es erneut): %s",
                  symbol, e)


def _cancel_protection(con, symbol: str) -> None:
    """Offene Schutz-Order stornieren (vor einem regulären Verkauf — die vom
    Stop reservierten Stücke wären sonst nicht verkäuflich)."""
    row = db.open_protection_order(con, symbol)
    if row is None:
        return
    if row["alpaca_id"]:
        try:
            broker.cancel_order(row["alpaca_id"])
        except broker.BrokerError as e:
            log.warning("%s: cancel der Schutz-Order fehlgeschlagen: %s", symbol, e)
            return
    db.upsert_order(con, {**{k: row[k] for k in row.keys()
                             if k in ("client_order_id", "alpaca_id", "symbol", "side",
                                      "notional", "submitted_at", "filled_qty",
                                      "filled_avg_price", "filled_at", "error")},
                          "status": "canceled"})
    time.sleep(1.5)  # Alpaca braucht einen Moment, bis die Stücke wieder frei sind
    log.info("%s: Schutz-Order storniert (%s)", symbol, row["client_order_id"])


def _pdt_blocked(con, symbol: str, day: str, equity: float) -> bool:
    """Pattern-Day-Trader-Schutz: Auf einem LIVE-Konto unter 25.000 USD Equity
    keinen Verkauf platzieren, wenn das Symbol heute schon gekauft wurde —
    jeder Same-Day-Roundtrip zählt als Daytrade und kann das Konto sperren.
    Paper-Accounts sind nicht betroffen."""
    if "paper" in config.ALPACA_BASE_URL or equity >= 25_000:
        return False
    iso_day = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    row = con.execute(
        "SELECT 1 FROM orders WHERE symbol=? AND side='buy' AND status='filled' "
        "AND substr(COALESCE(filled_at, submitted_at), 1, 10)=? LIMIT 1",
        (symbol, iso_day)).fetchone()
    return row is not None

def trade(con, as_of: str, timeframe: str = "1d") -> None:
    # Pott = Equity / ALLE Auto-Trade-Symbole (über beide Timeframes hinweg),
    # gehandelt werden in diesem Run nur die mit passendem effektivem Timeframe
    # (Lock am Symbol, ohne Lock der TRADING_TIMEFRAME-Fallback).
    all_symbols = db.autotrade_symbols(con)
    symbols = db.autotrade_symbols(con, timeframe=timeframe,
                                   fallback_tf=config.TRADING_TIMEFRAME)
    if not symbols:
        log.info("no autotrade symbols for timeframe %s — nothing to do", timeframe)
        return

    acct = broker.get_account()
    if acct.get("trading_blocked"):
        log.error("account is trading_blocked — skipping all orders")
        return
    positions = broker.get_positions()
    risks = db.autotrade_risk(con)
    pot = acct["equity"] / len(all_symbols)
    cash = acct["cash"]
    day = as_of[:10].replace("-", "")
    # 1h-Läufe handeln mehrmals täglich → Idempotenz-Fenster ist der Run
    # (UTC-Stunde), nicht der Tag; sonst wäre z. B. ein Re-Entry nach einem
    # Vormittags-Verkauf für den Rest des Tages blockiert.
    run_key = day if timeframe == "1d" else f"{day}T{as_of[11:13]}"
    log.info("trade[%s]: %d symbols, pot=%.2f (equity %.2f / %d), cash=%.2f",
             timeframe, len(symbols), pot, acct["equity"], len(all_symbols), cash)

    placeholders = ",".join("?" * len(symbols))
    signals = {r["symbol"]: r["signal"] for r in con.execute(
        f"SELECT symbol, signal FROM analysis WHERE as_of=? AND timeframe=? "
        f"AND symbol IN ({placeholders})",
        (as_of, timeframe, *symbols))}

    n_orders = 0
    for sym in symbols:
        sig = signals.get(sym)
        if sig is None:
            log.warning("%s: no analysis for this run — skipped", sym)
            continue
        if n_orders >= config.MAX_ORDERS_PER_RUN:
            log.error("MAX_ORDERS_PER_RUN (%d) reached — remaining symbols skipped",
                      config.MAX_ORDERS_PER_RUN)
            break

        if sig == "BUY" and sym not in positions:
            cid = f"spm-{sym}-{run_key}-buy"
            if db.order_exists(con, cid):
                log.info("%s: buy already submitted today (%s)", sym, cid)
                continue
            budget = round(min(pot, cash), 2)
            if budget < max(MIN_NOTIONAL, 0.01 * pot):
                log.warning("%s: cash %.2f zu klein für Pott %.2f — skipped",
                            sym, cash, pot)
                db.upsert_order(con, _order_row(
                    None, cid, sym, "buy", budget,
                    error=f"insufficient cash ({cash:.2f} for pot {pot:.2f})"))
                continue
            risk = risks.get(sym)
            try:
                if risk:
                    # Mit Risk-Config: ganze Stückzahlen kaufen — Alpaca
                    # unterstützt Stop-/Trailing-Orders nicht auf Bruchstücke.
                    last = db.last_close(con, sym, timeframe)
                    if not last:
                        log.warning("%s: kein Kurs für Stückzahl-Sizing — skipped", sym)
                        continue
                    qty = int(budget // last)
                    if qty < 1:
                        log.warning("%s: Pott %.2f reicht nicht für 1 Stück @ %.2f "
                                    "— skipped", sym, budget, last)
                        db.upsert_order(con, _order_row(
                            None, cid, sym, "buy", budget,
                            error=f"pot {budget:.2f} < 1 share @ {last:.2f}"))
                        continue
                    est = round(qty * last, 2)
                    o = broker.submit_qty_buy(sym, qty, cid)
                    db.upsert_order(con, _order_row(o, cid, sym, "buy", est))
                    cash -= est
                    n_orders += 1
                    log.info("%s: BUY %d Stk. (~%.2f USD) submitted (%s)",
                             sym, qty, est, o.get("status"))
                    # Fill kurz pollen, damit die Schutz-Order sofort steht —
                    # klappt es nicht rechtzeitig, übernimmt der nächste sync.
                    if _pdt_blocked(con, sym, day, acct["equity"]):
                        log.warning("%s: Schutz-Order heute übersprungen (PDT-Schutz, "
                                    "Live-Konto < 25k) — sync platziert sie morgen", sym)
                    else:
                        for _ in range(FILL_POLL_TRIES):
                            time.sleep(FILL_POLL_PAUSE)
                            upd = broker.get_order_by_client_id(cid)
                            if upd and upd.get("status") == "filled":
                                db.upsert_order(con, _order_row(upd, cid, sym, "buy", est))
                                entry = float(upd.get("filled_avg_price") or last)
                                filled = int(float(upd.get("filled_qty") or qty))
                                _place_protection(con, sym, filled, risk, entry, run_key)
                                break
                        else:
                            log.info("%s: Fill noch offen — Schutz-Order kommt beim "
                                     "nächsten sync", sym)
                else:
                    o = broker.submit_notional_buy(sym, budget, cid)
                    db.upsert_order(con, _order_row(o, cid, sym, "buy", budget))
                    cash -= budget
                    n_orders += 1
                    log.info("%s: BUY %.2f USD submitted (%s)", sym, budget, o.get("status"))
            except broker.BrokerError as e:
                db.upsert_order(con, _order_row(None, cid, sym, "buy", budget,
                                                error=str(e)))
                log.error("%s: buy failed: %s", sym, e)

        elif sig == "SELL" and sym in positions:
            cid = f"spm-{sym}-{run_key}-sell"
            if db.order_exists(con, cid):
                log.info("%s: sell already submitted this run window (%s)", sym, cid)
                continue
            if _pdt_blocked(con, sym, day, acct["equity"]):
                log.warning("%s: sell skipped — Same-Day-Roundtrip auf Live-Konto "
                            "< 25k USD (Pattern-Day-Trader-Schutz)", sym)
                continue
            # Offene Schutz-Order zuerst stornieren — sie hält die Stücke
            _cancel_protection(con, sym)
            qty = positions[sym]["qty_raw"]
            try:
                o = broker.submit_market_sell(sym, qty, cid)
                db.upsert_order(con, _order_row(o, cid, sym, "sell"))
                n_orders += 1
                log.info("%s: SELL %s Stk. submitted (%s)", sym, qty, o.get("status"))
            except broker.BrokerError as e:
                db.upsert_order(con, _order_row(None, cid, sym, "sell", error=str(e)))
                log.error("%s: sell failed: %s", sym, e)

    log.info("trade complete: %d order(s) submitted", n_orders)
