#!/usr/bin/env bash
# Manuelles Deployment (und Grundlage fuer CI/CD): Code holen, Web + Worker
# bauen/synchronisieren, Dienste neu starten. Als root auf dem Server ausfuehren:
#   ssh linux 'bash /opt/stocks-src/deploy/deploy.sh'
#
# Fasst die DB (/var/lib/stocks) nie an — Kurse/Analysen/Watchlist ueberleben.
set -euo pipefail

# --- Konfiguration -------------------------------------------------------
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # Repo-Wurzel (…/stocks-src)
TARGET=/var/www/stocks
SERVICE_USER=stocks

echo "==> git pull ($SRC)"
git -C "$SRC" pull --ff-only

echo "==> Web bauen -> $TARGET/web"
dotnet publish "$SRC/web/StocksWeb.csproj" -c Release -o "$TARGET/web"

echo "==> Worker synchronisieren -> $TARGET/worker"
rsync -a --delete --exclude __pycache__ "$SRC/worker/" "$TARGET/worker/"

echo "==> Python-Abhaengigkeiten"
"$TARGET/venv/bin/pip" install --quiet -r "$SRC/worker/requirements.txt"

echo "==> Rechte"
chown -R "$SERVICE_USER:$SERVICE_USER" "$TARGET/web" "$TARGET/worker"

echo "==> Dienste neu starten"
systemctl restart stocks-web stocks-worker

echo "==> Health-Check"
sleep 3
systemctl is-active stocks-web stocks-worker
curl -fsS -o /dev/null -w "web HTTP %{http_code}\n" http://127.0.0.1:5000/ \
  || echo "WARN: Web antwortet (noch) nicht auf 5000"
echo "==> fertig"
