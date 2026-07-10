# stocks.playmations.com

Selbstgehostetes Stock-Analyse-Dashboard. Voller Kontext: `docs/PROJEKTPLAN.md`.

## Architektur (Kurzfassung)

- `worker/` — Python 3.12. Fetcht Yahoo (yfinance) + optional FRED, rechnet mit den
  **unveränderlichen** Kern-Skripten `indicators.py` / `score.py` / `macro_pillar.py`
  (deterministisch, stdlib-only — Numerik niemals in C#/JS nachbauen) und schreibt
  nach `data/stocks.db` (SQLite, WAL). Einziger Schreiber für Analyse-Tabellen.
  Signale kommen aus austauschbaren Plugins in `worker/strategies/` (Kontrakt:
  dortiges `__init__.py`; Default `three_pillars` wrappt score.py). Die via
  `strategy_config`-Tabelle aktive Strategie steuert den täglichen Run.
- `worker/backtest.py` — stdlib-only, DB strikt read-only, importiert kein
  yfinance. Wird vom Web on-demand gespawnt (`list` bzw.
  `run SYMBOL --strategy … --params … --db …`), liefert Signale/Trades/Statistik
  als JSON auf stdout; Ergebnisse werden nicht persistiert (MemoryCache im Web).
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
