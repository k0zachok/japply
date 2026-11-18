#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu server (Oracle Cloud "Always Free" VM). Run as the default user:
#   bash ~/jobpilot/deploy/server_setup.sh
# Installs Python, the project's packages and Chromium, and a daily 08:30 (Vienna time) run via systemd.
set -euo pipefail
cd "$(dirname "$0")/.."
sudo timedatectl set-timezone Europe/Vienna
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip sqlite3 fonts-dejavu fonts-liberation
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
sudo .venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium

# keys and passwords: a file only this user can read (there is no macOS Keychain on the server)
if [ ! -f ~/.jobman.env ]; then
  printf 'JOBPILOT_API_KEY=\nJOBPILOT_MAILBOX_PASSWORD=\nJOBPILOT_EMAIL_PASSWORD=\nJOBPILOT_PORTAL_PASSWORD=\n' > ~/.jobman.env
fi
chmod 600 ~/.jobman.env

sudo tee /etc/systemd/system/jobman.service >/dev/null <<UNIT
[Unit]
Description=JobMan daily run
After=network-online.target
[Service]
Type=oneshot
User=$USER
WorkingDirectory=$PWD
EnvironmentFile=$HOME/.jobman.env
ExecStart=/bin/bash -c '$PWD/.venv/bin/python -m jobpilot run >> $PWD/run.log 2>&1'
TimeoutStartSec=3600
UNIT
sudo tee /etc/systemd/system/jobman.timer >/dev/null <<UNIT
[Unit]
Description=JobMan daily at 08:30
[Timer]
OnCalendar=*-*-* 08:30:00
Persistent=true
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now jobman.timer
echo
echo "Done. Next run: $(systemctl list-timers jobman.timer --no-pager | sed -n 2p)"
echo "Fill in ~/.jobman.env if the Mac didn't already copy your keys (deploy/push.sh does)."
