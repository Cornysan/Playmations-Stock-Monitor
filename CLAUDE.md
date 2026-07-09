# stocks.playmations.com

Selbstgehostetes Stock-Analyse-Dashboard. Voller Kontext: `docs/PROJEKTPLAN.md`.

## Architektur (Kurzfassung)

- `worker/` — Python 3.12. Fetcht Yahoo (yfinance) + optional FRED, rechnet mit den
  **unveränderlichen** Kern-Skripten `indicators.py` / `score.py` / `macro_pillar.py`
  (deterministisch, stdlib-only — Numerik niemals in C#/JS nachbauen) und schreibt
  nach `data/stocks.db` (SQLite, WAL). Einziger Schreiber für Analyse-Tabellen.
- `web/` — ASP.NET Core (.NET 10) Minimal-API + statisches Frontend
  (`wwwroot/`, Alpine.js + Lightweight Charts v5, vendored). Liest die DB;
  schreibt nur `watchlist` (add/remove/holding).
- `deploy/` — systemd-Units, Caddyfile, Server-Setup (`deploy/README.md`).
  CI/CD: `.github/workflows/deploy.yml`, self-hosted Runner auf dem Server.

## Lokale Entwicklung (Windows)

```powershell
# Worker (venv liegt in .venv/, PYTHONUTF8 wegen ═/▲ in CLI-Ausgaben)
$env:PYTHONUTF8='1'
.venv\Scripts\python.exe worker\main.py run [--only AAPL,MSFT]

# Web → http://127.0.0.1:5000
dotnet run --project web --no-launch-profile --urls http://127.0.0.1:5000
```

## Konventionen

- Ausnahme im Frontend-Grundsatz "nur rendern": die EMA-Chartlinien werden in
  `Program.cs` berechnet (gleiche SMA-Seed-Konvention wie `indicators.py`) —
  reine Visualisierung, Entscheidungszahlen kommen ausschließlich vom Worker.
- Höflichkeit ggü. Yahoo ist Pflicht (Plan §4.3): Batch-Downloads, Jitter,
  inkrementell, bei 429 Lauf abbrechen statt retryen.
- Secrets (`.env`, FRED-Key) nie committen.
