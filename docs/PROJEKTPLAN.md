# Projektplan – stocks.playmations.com

Ein selbstgehostetes Stock-Analyse-Dashboard im Stil von TradingView / Trade Republic.
Web-App in **ASP.NET Core**, Analyse durch einen **Python-Worker**, der die vorhandenen
Skripte (`indicators.py`, `score.py`, `macro_pillar.py`) wiederverwendet. Läuft auf dem
eigenen Linux-Server unter `stocks.playmations.com`, Deployment per CI/CD nach jedem Commit.

> Status: Planungsdokument / Spec. Gedacht als Startpunkt für die Umsetzung mit Claude Code (Fable 5).
> „Rudimentär zuerst" – Fokus auf lauffähiges Grundgerüst, keine aufwendige Absicherung/Auth in Phase 1.

---

## 0. Getroffene Entscheidungen (Stand jetzt)

- **Architektur:** Python-Worker rechnet, SQLite als geteilter Speicher, ASP.NET Core liest & rendert nur.
- **Frontend:** ASP.NET Core Razor Pages + Minimal-API, TradingView **Lightweight Charts**, Alpine.js für Interaktivität.
- **Datenquelle:** **Yahoo / yfinance als einzige Quelle** – für Kurse *und* Symbol-Suche. Ein Universum,
  damit kein Symbol-Mismatch zwischen Autocomplete und Fetch entstehen kann. Kostenlos, deckt die
  bestehende (internationale/OTC-)Ticker-Liste ab.
- **Kein Live-Preis.** Analyse **1×/Tag nach US-Close**; optionaler, gepolsterter Preis-Refresh alle ~2–3 h.
- **DB:** SQLite (WAL-Modus).
- **Yield-Spread (10Y-2Y):** optional über **FRED** (`T10Y2Y`, ein Call/Tag, offizielles freies API).
  Fällt er weg, verteilt `macro_pillar.py` das Gewicht automatisch um.

---

## 1. Ziele & Kernanforderungen

- **Watchlist** mit Symbolen, über das UI editierbar (hinzufügen / entfernen).
- **Autocomplete** beim Hinzufügen: nur real existierende Symbole werden vorgeschlagen.
- **Klick auf ein Symbol → Chart** (Candlesticks im TradingView-Stil, mit EMA-Overlays).
- **Action-Badge direkt am Ticker** (Buy / Hold / Sell / …) aus `score.py`, farbcodiert.
- **Progressive Disclosure**: viele Infos, aber nur auf Wunsch sichtbar. Hover/Klick auf ein
  Badge zeigt die **Compound-Werte** (Pillar-Scores, Flags, Rohindikatoren), die zur
  Entscheidung geführt haben.
- **Hintergrund-Prozess** fetcht & analysiert periodisch – **robust & höflich** gegenüber Yahoo,
  nach dem Muster des bestehenden Tiingo-Monitors.
- Modernes, dunkles UI.

---

## 2. Architektur

Die drei Skripte sind reine stdlib-Python-Module, deterministisch, und `score.py` sagt explizit:
die Zahlen **nicht** durch „Reasoning" neu berechnen. Deshalb bleibt die Numerik in Python, und die
Web-App berechnet selbst **keine** Indikatoren – sie liest nur, was der Worker in SQLite geschrieben hat.

```
                       ┌────────────────────────────────────────────────┐
                       │                Linux-Server                     │
                       │                                                 │
  Yahoo / yfinance ──▶ │  Python-Worker (systemd)                        │
  FRED (10Y-2Y, opt.)─▶│    fetch → indicators.py → score.py             │
                       │           macro_pillar.py                       │
                       │                    │                            │
                       │                    ▼                            │
                       │              stocks.db  (SQLite, WAL)           │
                       │                    ▲                            │
                       │                    │ (nur lesen)                │
                       │  ASP.NET Core (Kestrel, systemd) ──┐            │
                       │                                    │            │
                       │  Caddy (Reverse Proxy, Auto-HTTPS) ┘            │
                       └────────────────────────────────────────────────┘
                                          │
                                          ▼
                          https://stocks.playmations.com
```

---

## 3. Tech-Stack

- **Worker:** Python 3.11. Abhängigkeiten: `yfinance`, `requests`, `pandas`, `curl_cffi`
  (browserechte Session gegen Bot-Erkennung). Bindet `indicators.py`, `score.py`,
  `macro_pillar.py` als Module ein.
- **DB:** **SQLite** (WAL). Ein Schreiber (Worker), mehrere Leser (Web). Kein separater DB-Dienst nötig.
- **Web:** ASP.NET Core (.NET 8 LTS), **Razor Pages** + Minimal-API für JSON-Endpunkte.
- **Charts:** **TradingView Lightweight Charts** (MIT, kostenlos). Candlesticks + EMA20/50/200;
  RSI/MACD optional in Sub-Panes.
- **Frontend-Interaktivität:** **Alpine.js** (Accordions, Tooltips) + `fetch()` gegen die API.
  Kein schweres SPA-Framework.
- **Reverse Proxy:** **Caddy** (Auto-TLS für die Domain).
- **CI/CD:** GitHub + **self-hosted GitHub-Actions-Runner** auf dem Server (baut *auf* dem Server).

---

## 4. Datenquelle & Fetch-Strategie (Yahoo)

### 4.1 Quellen & Symbole
- **Kursdaten (OHLC daily):** yfinance (`yf.download([...])` im Batch, nicht Ticker-für-Ticker-Schleife).
  - `score.py`/`indicators.py` brauchen **≥ 220 Daily-Bars** (ideal ~290) für EMA200 → Lookback ~1 Jahr
    (nur beim ersten Mal; danach inkrementell, siehe 4.2).
- **Symbol-Suche/Validierung:** Yahoo Search-Endpoint
  (`https://query1.finance.yahoo.com/v1/finance/search?q=…`, bzw. `yf.Search` in neueren yfinance-Versionen).
  Speist die Autocomplete → schlägt nur existierende Yahoo-Ticker vor.
  - **Wichtig:** Da Kurse *und* Suche aus derselben Quelle kommen, ist jedes vorgeschlagene Symbol
    garantiert fetchbar – **kein Mapping, kein Mismatch**. Die bestehende Ticker-Schreibweise
    (`B4B.HM`, `BAYZF`, `POAHF`, `DLAKY`, …) funktioniert unverändert.
- **Macro-ETFs** (für `macro_pillar.py`): `SPY, RSP, IWM, HYG, LQD, TLT, XLY, XLP` – ebenfalls über Yahoo.
  Werden **1×/Tag** geholt und für **alle** Symbole wiederverwendet (ein Macro-Score pro Lauf, injiziert
  in jeden `score_symbol`-Aufruf).
- **Yield-Spread 10Y-2Y (optional):** FRED-Serie `T10Y2Y` (freier Key, ein Call/Tag). Ohne ihn verteilt
  `macro_pillar.score_macro` das 20%-Gewicht der Kurve automatisch um.

### 4.2 Kadenz (kein Live-Preis)
Yahoo hat **keine offiziellen Limits** – geblockt wird intransparent nach IP/Muster (429). Deshalb ist
das Ziel „robust", nicht „maximale Frequenz". Da die Signale auf **Daily-Bars** beruhen (ändern sich nur
1×/Tag nach Close), gilt:

- **Analyse:** **1× täglich nach US-Close** – ein voller Lauf über die Watchlist. Das ist alles, was die
  Signale datenseitig hergeben; häufiger würde nur dieselben, schon geschlossenen Kerzen neu verarbeiten.
- **Optionaler Preis-Refresh** (frischere, noch offene Tageskerze) alle **~2–3 h** während der Handelszeit,
  in gestaffelten Mini-Batches – Kadenz gespiegelt vom bestehenden Monitor (Batches à 5, 3,5–6,5 s pro
  Ticker, ~10 min zwischen Batches; für ~90 Ticker ≈ ein Durchlauf alle 2–3 h). Das läuft dort bereits stabil.

### 4.3 Schutzmechanismen gegen Blocking (Pflicht)
- **Caching / inkrementell:** Historie liegt in `bars`; pro Tag nur die **neue** Kerze nachladen, nie die
  volle Historie erneut ziehen.
- **Batch-Download:** `yf.download([...])` statt Einzelaufrufe → drastisch weniger HTTP-Calls.
- **Exponentielles Backoff bei 429 – hart:** Ein 429 bedeutet „für den Rest des Fensters pausieren", nicht
  sofort neu versuchen.
- **Browserechte Session:** `curl_cffi` mit `impersonate` + rotierende User-Agents.
- **Graceful Degradation:** Bei Fehler letzten guten Wert behalten und anzeigen, statt zu hämmern.
- **Jitter/Pausen** zwischen Requests (wie `process_ticker` im Monitor).

> Hinweis: Die yfinance-Quelle ist inoffiziell und kann sich jederzeit ändern/blocken. Der Worker muss
> Ausfälle folgenlos wegstecken (letzter Stand bleibt sichtbar, nächster Lauf versucht es erneut).

---

## 5. Datenbank-Schema (SQLite)

```sql
-- Watchlist (vom UI editierbar)
CREATE TABLE watchlist (
  symbol      TEXT PRIMARY KEY,
  name        TEXT,
  enabled     INTEGER NOT NULL DEFAULT 1,
  holding     INTEGER NOT NULL DEFAULT 0,   -- 0 = flat (Entry-Framing), 1 = im Depot
  added_at    TEXT NOT NULL
);

-- OHLC-Bars fürs Chart + für die Indikatoren
CREATE TABLE bars (
  symbol TEXT NOT NULL,
  date   TEXT NOT NULL,          -- ISO YYYY-MM-DD
  open   REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY (symbol, date)
);

-- Neueste Analyse pro Symbol (plus Historie via as_of)
CREATE TABLE analysis (
  symbol         TEXT NOT NULL,
  as_of          TEXT NOT NULL,     -- Zeitstempel des Laufs
  trend_score    INTEGER,
  momentum_score INTEGER,
  macro_score    INTEGER,
  pillar_total   INTEGER,
  action         TEXT,              -- z.B. "EXIT / TRIM"
  rationale      TEXT,
  framing        TEXT,
  flags_json     TEXT,              -- {exhaustion:[], bearish:[], rebound:[], death_cross, stretch_pct}
  indicators_json TEXT,             -- I._round(ind): ema/rsi/macd/trix/bollinger …
  PRIMARY KEY (symbol, as_of)
);

-- Aktueller Macro-Snapshot (einer für den ganzen Lauf)
CREATE TABLE macro_snapshot (
  as_of          TEXT PRIMARY KEY,
  composite      REAL,
  regime         TEXT,
  pillar_score   INTEGER,
  pillar_label   TEXT,
  components_json TEXT,
  notes_json     TEXT
);

-- Fetch-Buchhaltung / Zustände (letzte Läufe, Backoff-Fenster etc.)
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
```

Web liest v. a. den jeweils **neuesten** `analysis`-Eintrag pro Symbol (`MAX(as_of)`), die `bars` fürs
Chart und den letzten `macro_snapshot`.

---

## 6. Python-Worker

Verzeichnis `worker/`. Wiederverwendung der Skripte **1:1** (als Module importiert).

Ablauf pro Tages-Lauf (nach US-Close):
1. Macro: ETFs (Yahoo) + FRED-Spread (optional) → `macro_pillar.score_macro(...)` → `macro_snapshot`
   schreiben, `pillar_score` merken.
2. Pro aktivem Watchlist-Symbol:
   - Daily-Bars inkrementell aktualisieren (Cache in `bars`), Close-Liste alt→neu bilden.
   - `score.py`:
     ```python
     card = score_symbol(closes, macro_score=macro_pillar_score,
                          symbol=sym, holding=bool(row.holding))
     ```
   - `card` in `analysis` schreiben (Scores, action, rationale, framing, flags_json, indicators_json).
   - Schutzmechanismen aus 4.3 respektieren.
3. Optionaler Preis-Refresh-Loop (alle ~2–3 h während Handelszeit): nur letzten Preis/aktuelle Kerze
   aktualisieren, **keine** Voll-Analyse.

Läuft als `systemd`-Dienst mit Auto-Restart. Logging in Datei + journalctl.

> Hinweis: `score.py`'s `decide()` nutzt `holding`. Default `holding=False/None` → **Entry-Framing**
> (Kaufen/Meiden statt Halten/Verkaufen). Das `holding`-Flag pro Symbol (UI-Toggle, Phase 6) schaltet
> auf Holder-Framing um.

---

## 7. Web-API (ASP.NET Core, JSON)

| Methode | Route | Zweck |
|---|---|---|
| GET | `/api/watchlist` | Liste inkl. neuestem action/Score-Badge + letztem Preis |
| POST | `/api/watchlist` `{symbol}` | Symbol hinzufügen (validiert via Yahoo-Search) |
| DELETE | `/api/watchlist/{symbol}` | Symbol entfernen |
| PATCH | `/api/watchlist/{symbol}` `{holding}` | Depot-Flag umschalten (Phase 6) |
| GET | `/api/symbols/search?q=` | Autocomplete – nur existierende Yahoo-Ticker (gecacht) |
| GET | `/api/symbols/{symbol}/bars?range=1y` | OHLC fürs Chart |
| GET | `/api/symbols/{symbol}/analysis` | Volle Aufschlüsselung (Pillars, Flags, Rohindikatoren) |
| GET | `/api/macro` | Aktueller Macro-Snapshot |

---

## 8. Frontend / UI

### 8.1 Watchlist-Ansicht
Zeilen: `Ticker · Name · letzter Preis · Action-Badge · Pillar-Total (−6..+6)`.
Klick auf Zeile → Detailansicht. Hover auf Badge → Kurz-Tooltip mit den 2–3 stärksten Flags.

**Action → Farbe** (aus `score.py`-Actions):

| Action | Farbe |
|---|---|
| `RE-ENTRY (new cycle)` | kräftiges Grün |
| `HOLD (ride the cycle)` | Grün |
| `TACTICAL REBOUND (counter-trend)` | Teal/Amber (opportunistisch) |
| `WAIT (do not chase)` / `HOLD (under review)` | Amber |
| `EXIT / TRIM` / `EXIT` | Rot |
| `STAY OUT / AVOID` | Grau-Rot |
| `OBSERVE` / `HOLD / OBSERVE` | Grau |

### 8.2 Detailansicht (Symbol)
- **Lightweight Charts** Candlesticks + EMA20/50/200-Overlays; optional RSI-/MACD-Sub-Pane.
- **Action-Banner** oben (Farbe wie oben) mit `rationale` + `framing`.
- **Progressive Disclosure** (Accordions, standardmäßig eingeklappt) – „Wie kam das Signal zustande?":
  - *Pillars:* Trend (Detail-Bits: `price>EMA20`, `EMA20>EMA50`, …), Momentum (RSI/MACD-Hist/TRIX),
    Macro (injizierter Score + Regime).
  - *Flags:* `exhaustion`, `bearish`, `rebound`, `death_cross`, `stretch_pct`.
  - *Rohindikatoren:* die Werte aus `indicators_json` (EMA/RSI/MACD/TRIX/Bollinger/%B).
- Alles kommt fertig aus `analysis` – reines Rendering, keine Berechnung im Frontend.

### 8.3 Watchlist bearbeiten
- Suchfeld mit Autocomplete gegen `/api/symbols/search` → nur existierende Ticker.
- Auswahl → `POST /api/watchlist` → Worker nimmt das Symbol im nächsten Lauf auf.

### 8.4 Look
Dunkles Theme, monospace für Zahlen, dezente Grün/Rot-Akzente. Clean, TradingView-/Trade-Republic-nah.

> UI-Hinweis: Die Actions sind mechanische Ausgaben deiner eigenen Formeln, keine Finanzberatung –
> ein kleiner Disclaimer-Fußzeilentext schadet nicht.

---

## 9. Deployment & CI/CD

### 9.1 Dienste (systemd)
- `stocks-web.service` → Kestrel auf `127.0.0.1:5000`.
- `stocks-worker.service` → Python-Worker (venv), Auto-Restart.

### 9.2 Reverse Proxy (Caddy)
```
stocks.playmations.com {
    reverse_proxy 127.0.0.1:5000
}
```
Caddy holt/erneuert das TLS-Zertifikat automatisch.

### 9.3 Pipeline (self-hosted Runner auf dem Server)
Bei Push auf `main`:
1. Checkout.
2. `dotnet publish -c Release -o /var/www/stocks` (SDK liegt auf dem Server).
3. Worker: `pip install -r worker/requirements.txt` in die venv.
4. `systemctl restart stocks-web stocks-worker`.

### 9.4 Secrets / Config
FRED-Key etc. in eine **nicht committete** `.env` / `appsettings.Production.json` (`.gitignore`!).
Auch in der rudimentären Phase gehören Keys nicht ins Repo.

> ⚠️ Praktischer Hinweis: In `Tiingo3.py` stehen ein echter **Tiingo-API-Key** und ein
> **Telegram-Bot-Token** im Klartext. Da die Datei hochgeladen wurde, wäre es klug, beide zu **rotieren**.
> (Für dieses Projekt wird Tiingo nicht mehr gebraucht – Yahoo ist die Quelle.)

---

## 10. Umsetzung in Phasen (Vorschlag)

- **Phase 0 – Gerüst & Pipeline:** Repo, systemd-Units, Caddy, self-hosted Runner. Ziel:
  „Hello World"-Deploy läuft end-to-end nach Commit.
- **Phase 1 – Worker:** Yahoo-Fetch (Watchlist-Daily + Macro-ETFs + FRED-Spread), Skripte einbinden,
  SQLite schreiben, Schutzmechanismen (Cache/Backoff). CLI-prüfbar.
- **Phase 2 – Read-API + Watchlist-Seite** mit Action-Badges.
- **Phase 3 – Detailseite + Lightweight Charts** (Candles + EMA-Overlays).
- **Phase 4 – Progressive Disclosure** (Breakdown-Accordions, Hover-Tooltips, Macro-Panel).
- **Phase 5 – Watchlist editieren** + Yahoo-Autocomplete.
- **Phase 6 – Politur:** Dark-Theme-Feinschliff, Responsive, `holding`-Toggle (Holder-Framing),
  optionaler Preis-Refresh-Loop.

---

## 11. Strategien & Backtesting (Nachtrag Juli 2026)

Signal-Logik ist seit Juli 2026 austauschbar (à la TradingView-Strategien, aber ohne
Code-Editing in der UI — kein Remote-Code-Execution-Risiko):

- **Plugins:** `worker/strategies/<name>.py` mit `NAME/LABEL/DESCRIPTION/PARAMS` und
  `decide(closes, holding, params, macro_score) → card` (bewertet den letzten Bar;
  Kontrakt im Package-`__init__.py`). Default `three_pillars` wrappt das unveränderte
  `score.py`; `ema_cross` dient als Vorlage. Neue Strategien = neue Datei im Repo,
  die UI editiert ausschließlich die deklarierten Parameter.
- **Aktive Strategie:** Tabelle `strategy_config` (name, params_json, active).
  Genau eine aktiv (ohne Eintrag: `three_pillars`); sie erzeugt die täglichen
  Actions/Badges. `analysis` hat dafür die Spalten `strategy` + `signal`
  (normalisiert BUY/HOLD/SELL). Web darf `strategy_config` schreiben (admin-only) —
  neben `watchlist` die einzige Ausnahme vom Single-Writer-Prinzip.
- **Backtests:** `worker/backtest.py` (stdlib-only, DB read-only) iteriert `decide()`
  über die Historie mit simulierter Position. Ausführung Default **next_open**
  (wie Live), eine Position all-in/all-out; Macro-Säule historisch nicht
  verfügbar (`macro_score=None`). Ergebnis: `signals` (Chart-Marker ▲▼), `trades`,
  `stats` (Rendite, Buy&Hold, Win-Rate, Profit-Faktor, Max Drawdown, Exposure),
  `equity`-Kurve. On-demand vom Web gespawnt (Config `PythonPath`), 1 h MemoryCache,
  nichts persistiert.
- **Slippage:** `--slippage-bps` (Default 5 = 0,05 % je Fill-Seite) verschlechtert
  jeden Fill — Käufe teurer, Verkäufe/Stops billiger; der Buy&Hold-Vergleich zahlt
  seinen Einstiegs-Fill mit. 0 = idealisierte alte Rechnung. UI-Feld „Slip bp".
- **Historie & Sweep (Nachtrag):** Tagesbars werden 5 Jahre vorgehalten
  (`LOOKBACK_DAYS=1900`; `update_bars` merkt sich per Meta-Key
  `backfill_1d:{sym}`, mit welchem Fenster ein Symbol voll geladen wurde —
  ein Config-Bump backfillt beim nächsten Run automatisch). Die Analyse liest
  weiterhin nur die letzten ~320 Closes; lange Historie kostet nur
  Backtest-Laufzeit. `backtest.py sweep SYMBOL --split 0.7 [--grid JSON]`
  fährt einen Parameter-Sweep (Default: bis zu 8 Werte je Param aus dem
  PARAMS-Schema, Kappung bei 500 Kombinationen) mit Train/Test-Split:
  Ranking nach dem Trainings-Zeitraum, daneben das Out-of-Sample-Ergebnis
  samt `test_rank`. Großer Abstand Train-Rang ↔ Test-Rang = Overfitting;
  gesund ist ein flaches Plateau ähnlicher Params. CLI-only (kein Web-Endpoint).
- **Portfolio-Backtest:** `backtest.py portfolio [--symbols A,B,…]` (Default:
  Watchlist aus der DB) simuliert das Pott-Modell aus `trader.py` — ein
  Cash-Topf, Pott = Equity/N, Buys = min(Pott, Cash), all-in/all-out je Symbol,
  Ausführung immer next_open, Stops intra-Kerze. Liefert Gesamt-`stats`
  (inkl. `avg_invested_pct`), `per_symbol`-Beiträge (Prozentpunkte des
  Startkapitals) und die Portfolio-`equity`-Kurve; Benchmark = Pott gleich
  verteilt und liegen lassen. Symbole mit < 60 Bars werden übersprungen
  (`meta.skipped`).
- **API:** `GET /api/strategies`, `PUT /api/strategies/{name}` (admin; Params +
  aktiv setzen), `GET /api/symbols/{s}/backtest?strategy=&params=&risk=&slippage=`
  (alles wird gegen Schema/Grenzen validiert, bevor es den Python-Spawn erreicht),
  `GET /api/portfolio/backtest?strategy=&params=&tf=&risk=&slippage=` (ganze
  Watchlist, 300-s-Timeout statt 30 s — three_pillars × 100 Symbole × 5 Jahre
  braucht ~2 min).
- **UI:** Strategie-Dropdown + Parameter-Inputs überm Chart (Änderung → Backtest neu),
  Buy/Sell-Marker im Chart, Backtest-Accordion mit Statistik-Grid + Trade-Liste.
  Admin-Buttons „Speichern" / „Aktiv setzen". Startseite: Accordion
  „Portfolio-Backtest" (linke Spalte) mit Start-Button — nutzt die aktuell im
  Detail-Panel eingestellte Strategie-Konfiguration.

---

## 12. Auto-Trading via Alpaca Paper (Nachtrag Juli 2026)

Die Signale der aktiven Strategie werden optional automatisch gehandelt —
zunächst ausschließlich gegen einen Alpaca-**Paper**-Account (Live erfordert
bewusst eine andere `ALPACA_BASE_URL`).

- **Pott-Modell (Diversifikation):** `Pott = Konto-Equity / N` mit N = Anzahl
  der Symbole mit `watchlist.autotrade=1` (UI-Toggle, admin). Pro Symbol
  all-in/all-out: BUY im Flat → Notional-Market-Buy über den Pott (Bruchstücke),
  SELL in Position → Market-Sell der ganzen Position. Basis ist die Equity,
  nicht die gehebelte Margin-Buying-Power.
- **Ablauf:** `trader.sync` (vor der Analyse) refresht Order-Status, setzt
  `holding` der Auto-Trade-Symbole aus den echten Alpaca-Positionen und schreibt
  `broker_snapshot`. `trader.trade` (nach der Analyse) platziert Orders — nach
  US-Close eingereicht, füllt Alpaca sie zur nächsten Eröffnung. Bricht Yahoo
  den Run ab (Rate-Limit), wird nicht gehandelt.
- **Sicherungen:** Opt-in `TRADING_ENABLED=1`; `client_order_id` =
  `spm-{SYMBOL}-{YYYYMMDD}-{side}` (idempotent, von Alpaca unique-enforced);
  `MAX_ORDERS_PER_RUN` (Default 20); Broker-Fehler brechen nie die Analyse;
  fehlgeschlagene Orders (z. B. OTC-Titel) landen mit Fehlertext in `orders`.
- **Backtest-Angleichung:** Default-Ausführung jetzt `next_open` (wie Live);
  `--execution close` bleibt als optimistischer Vergleichsmodus.
- **API/UI:** `GET /api/trading` (Snapshot, Pott, Orderliste, enabled-Flag),
  `PATCH /api/watchlist/{s}` mit `autotrade`; UI: Auto-Trade-Toggle in der
  Detailansicht, gelber Punkt in der Watchlist, „Paper-Trading"-Accordion
  (Equity/Cash/Pott + Orders) auf der Startseite.
- **Secrets:** `ALPACA_KEY_ID`/`ALPACA_SECRET_KEY` in `.env` bzw.
  `/etc/stocks/env` — nie ins Repo.

---

## Anhang – Rollen der bestehenden Skripte

- **`indicators.py`** – deterministische Indikator-Engine (EMA 20/50/200, RSI-14 Wilder,
  MACD 12/26/9, TRIX-15, Bollinger 20/2). `compute(closes)` → Dict mit letzten Werten + Slopes.
- **`score.py`** – mappt Indikatoren auf **3 Pillars** (Trend / Momentum / Macro, je −2..+2) und
  leitet via Exhaustion/Rebound-Logik die **Action** ab. `score_symbol(closes, macro_score, symbol, holding)`.
- **`macro_pillar.py`** – Cross-Asset-Macro-Score aus ETF-Ratios + Yield-Spread → `pillar_score` (−2..+2),
  der in jeden Symbol-Score injiziert wird.
