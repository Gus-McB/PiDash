#!/usr/bin/env bash
# pi-dash installer. Run from the unpacked directory: sudo ./install.sh
set -euo pipefail

DEST=/opt/pi-dash
RUN_AS="${SUDO_USER:-$USER}"

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

echo "==> Installing to $DEST, running as $RUN_AS"
apt-get update -qq
apt-get install -y -qq python3-venv python3-dev

mkdir -p "$DEST"
cp -r server.py static requirements.txt "$DEST/"
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q --upgrade pip
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"
chown -R "$RUN_AS:$RUN_AS" "$DEST"

echo "==> Granting journal and GPU-sensor access to $RUN_AS"
usermod -aG systemd-journal,video "$RUN_AS"

echo "==> Allowing systemctl start/stop/restart without a password"
cat > /etc/sudoers.d/pi-dash <<SUDO
$RUN_AS ALL=(root) NOPASSWD: /usr/bin/systemctl start *, /usr/bin/systemctl stop *, /usr/bin/systemctl restart *
SUDO
chmod 440 /etc/sudoers.d/pi-dash
visudo -c -f /etc/sudoers.d/pi-dash >/dev/null

echo "==> Installing service"
sed "s/User=%i/User=$RUN_AS/" pidash.service > /etc/systemd/system/pidash.service
systemctl daemon-reload
systemctl enable --now pidash

sleep 3
TOKEN_PATH="$(getent passwd "$RUN_AS" | cut -d: -f6)/.config/pidash/token"
echo
echo "  pi-dash is running."
echo "  url    http://$(tailscale ip -4 2>/dev/null | head -1):8088/"
echo "  token  $(cat "$TOKEN_PATH" 2>/dev/null || echo '(check: journalctl -u pidash)')"
echo
echo "  Group changes need a re-login or reboot before vcgencmd and the journal work."
