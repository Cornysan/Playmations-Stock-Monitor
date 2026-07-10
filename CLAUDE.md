# stocks.playmations.com

Selbstgehostetes Stock-Analyse-Dashboard. Voller Kontext: `docs/PROJEKTPLAN.md`.

## Architektur (Kurzfassung)

- `worker/` — Python 3.12. Fetcht Yahoo (yfinance) + optional FRED, rechnet mit den
  **unveränderlichen** Kern-Skripten `indicators.py` / `score.py` / `macro_pillar.py`
  (deterministisch, stdlib-only — Numerik niemals in C#/JS nachbauen) und schreibt
  nach `data/stocks.db` (SQLite, WAL). Einziger Schreiber für Analyse-Tabellen.
  Signale kommen aus austauschbaren Plugins in `worker/strategies/` (Kontrakt:
  dortiges `__init__.py`; Default `three_pillars` wrappt score.py). Die in
  `strategy_config` (PK name+timeframe) je Timeframe aktive Strategie steuert
  die Analyse; Auto-Trade-Symbole mit Lock (watchlist.strat_name/-params/
  -timeframe, beim Aktivieren im UI eingefroren) werden stattdessen mit ihrer
  eingelockten Strategie analysiert und gehandelt.
  Zwei Timeframes: Tagesrun nach US-Close (Macro + `bars` + Analyse `1d`) und
  Stunden-Läufe Mo–Fr 10:35–16:35 ET (`bars_1h` + Analyse `1h`, nur fertige
  Kerzen, Macro aus letztem Snapshot). `analysis.timeframe` trennt beide.
- `worker/backtest.py` — stdlib-only, DB strikt read-only, importiert kein
  yfinance. Wird vom Web on-demand gespawnt (`list`,
  `run SYMBOL --strategy … --params … --timeframe 1d|1h --db …` bzw.
  `portfolio [--symbols …]` = Pott-Modell wie trader.py über die Watchlist),
  liefert Signale/Trades/Statistik als JSON auf stdout; Ergebnisse werden
  nicht persistiert (MemoryCache im Web). `--slippage-bps` (Default 5 je
  Fill-Seite, auch für den Buy&Hold-Einstieg) hält die Zahlen ehrlich; 0 = aus.
  `sweep SYMBOL --split 0.7 [--grid JSON]` (CLI-only) = Parameter-Sweep mit
  Train/Test-Split gegen Overfitting: Grid auf dem Train-Teil ranken, blind auf
  dem Test-Teil auswerten (`test_rank` weit unter Train-Rang = überangepasst).
  Tagesbars liegen 5 Jahre zurück (`LOOKBACK_DAYS`, Backfill-Marker
  `meta backfill_1d:*` — Fenster-Bump heilt sich beim nächsten Run selbst).
- `worker/broker.py` + `worker/trader.py` — Auto-Trading (Alpaca, Default Paper).
  Pott-Modell: Equity / N Auto-Trade-Symbole, all-in/all-out je Symbol nach
  `analysis.signal`. Opt-in via `TRADING_ENABLED=1` + Keys in `.env`;
  `TRADING_TIMEFRAME` (1d Default / 1h) bestimmt, ob der Tagesrun oder die
  Stunden-Läufe handeln. Idempotenz über `client_order_id` (Fenster = Tag bzw.
  UTC-Stunde; Tabellen `orders`, `broker_snapshot`); PDT-Schutz blockt
  Same-Day-Roundtrips auf Live-Konten < 25k USD. `holding` der
  Auto-Trade-Symbole wird aus echten Alpaca-Positionen gesynct.
- `web/` — ASP.NET Core (.NET 10) Minimal-API + statisches Frontend
  (`wwwroot/`, Alpine.js + Lightweight Charts v5, vendored). Liest die DB;
  schreibt nur `watchlist` (add/remove/holding) und `strategy_config`
  (Params speichern / aktiv setzen, admin-only). Config `PythonPath` zeigt auf
  den Worker-Python (Auto-Fallback: `.venv/` im Repo bzw. `venv/` am Server).
- `deploy/` — systemd-Units, Caddyfile, Server-Setup (`deploy/README.md`).
  CI/CD: `.github/workflows/deploy.yml`, self-hosted Runner auf dem Server.

## Lokale Entwicklung (Windows)

```powershell
# Worker (venv liegt in .venv/, PYTHONUTF8 wegen ═/▲ in CLI-Ausgaben)
$env:PYTHONUTF8='1'
.venv\Scripts\python.exe worker\main.py run [--only AAPL,MSFT]

# Web → http://127.0.0.1:5000 (Development lädt AdminPassword "dev" für den Login;
# App-Argumente müssen hinter das "--", sonst schluckt dotnet run sie)
dotnet run --project web --no-launch-profile -- --urls http://127.0.0.1:5000 --environment Development
```

## Konventionen

- Ausnahme im Frontend-Grundsatz "nur rendern": die EMA-Chartlinien werden in
  `Program.cs` berechnet (gleiche SMA-Seed-Konvention wie `indicators.py`) —
  reine Visualisierung, Entscheidungszahlen kommen ausschließlich vom Worker.
- Höflichkeit ggü. Yahoo ist Pflicht (Plan §4.3): Batch-Downloads, Jitter,
  inkrementell, bei 429 Lauf abbrechen statt retryen.
- Secrets (`.env`, FRED-Key) nie committen.
