#!/usr/bin/env bash
# Bootstrap: venv + пакет + systemd-юнит для linkforge.
# Запуск: sudo bash deploy/install.sh  (из корня репозитория)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=linkforge.service
VENV="$ROOT/.venv"

if [ "$(id -u)" -ne 0 ]; then
  echo "запусти от root: sudo bash $0"; exit 1
fi

command -v python3 >/dev/null || { apt-get update && apt-get install -y python3 python3-venv; }

python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -e "$ROOT"

if [ ! -f "$ROOT/.env" ]; then
  echo "ВНИМАНИЕ: $ROOT/.env не найден — скопируй .env.example и заполни ключи."
  echo "Без .env сервис стартовать не будет."
fi

mkdir -p "$ROOT/links"
install -m 0644 /dev/null "$ROOT/panel.log"

cat > /etc/systemd/system/$SERVICE <<EOF
[Unit]
Description=LinkForge farm (Google AI Pro links via Jio)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=PYTHONUTF8=1
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/linkforge bot -m 1440
Restart=always
RestartSec=15
StandardOutput=append:$ROOT/panel.log
StandardError=append:$ROOT/panel.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $SERVICE
echo
echo "готово: systemctl start $SERVICE && journalctl -u $SERVICE -f"
