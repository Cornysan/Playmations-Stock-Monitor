# Server-Setup (einmalig)

Zielbild aus dem Projektplan: Caddy → Kestrel (127.0.0.1:5000), Python-Worker als
Daemon, SQLite unter `/var/lib/stocks/stocks.db`, Deployment per self-hosted
GitHub-Actions-Runner bei jedem Push auf `main`.

## 1. Pakete

Caddy, rsync, git und Python 3.12 sind auf Ubuntu 24.04 bereits vorhanden.
Fehlt nur das .NET-10-SDK (nötig, weil auf dem Server gebaut wird):

```bash
# Microsoft-Paketquelle einbinden, dann SDK installieren
wget https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb -O /tmp/ms.deb
sudo dpkg -i /tmp/ms.deb
sudo apt update && sudo apt install -y dotnet-sdk-10.0
sudo apt install -y python3.12-venv   # falls venv-Modul fehlt
```

## 2. Nutzer & Verzeichnisse

```bash
sudo useradd -r -m -d /var/www/stocks -s /usr/sbin/nologin stocks
sudo mkdir -p /var/www/stocks/{web,worker} /var/lib/stocks /etc/stocks
sudo python3.12 -m venv /var/www/stocks/venv
sudo chown -R stocks:stocks /var/www/stocks /var/lib/stocks
```

## 3. Secrets (nicht ins Repo!)

```bash
# /etc/stocks/env
FRED_API_KEY=...        # optional, für den 10Y-2Y-Spread
AdminPassword=...       # Admin-Login der Web-App (ohne: reiner View-Modus)
```

Nach Änderungen an dieser Datei: `systemctl restart stocks-web stocks-worker`.

## 4. Dienste & Proxy

```bash
sudo cp deploy/stocks-web.service deploy/stocks-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stocks-web stocks-worker
```

Caddy läuft auf diesem Server bereits (serviert andere Seiten) — **nicht** die
`/etc/caddy/Caddyfile` überschreiben, sondern den Block aus `deploy/Caddyfile`
ans Ende der bestehenden Datei anhängen:

```bash
cat deploy/Caddyfile | sudo tee -a /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

DNS: `stocks.playmations.com` → Server-IP. Caddy holt das TLS-Zertifikat automatisch.

## 5. Self-hosted Runner

GitHub → Repo → Settings → Actions → Runners → "New self-hosted runner", den
Anweisungen folgen. Damit der Runner die Dienste neu starten darf (Workflow-Schritt
`sudo systemctl restart …`), braucht sein Nutzer eine NOPASSWD-Regel:

```bash
# /etc/sudoers.d/stocks-deploy  (Runner-Nutzer ggf. anpassen)
runner ALL=(root) NOPASSWD: /usr/bin/systemctl restart stocks-web stocks-worker
```

Außerdem Schreibrechte des Runner-Nutzers auf `/var/www/stocks` (z. B. via Gruppe).

## 6. Erste Befüllung

```bash
sudo -u stocks /var/www/stocks/venv/bin/python /var/www/stocks/worker/main.py run
```

Danach übernimmt `stocks-worker` (Loop: täglicher Lauf nach US-Close,
Catch-up-Check alle 15 min für neu hinzugefügte Symbole).
