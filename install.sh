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
cp -r server.py static requirements.txt services.example.json "$DEST/"
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q --upgrade pip
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"
chown -R "$RUN_AS:$RUN_AS" "$DEST"

echo "==> Granting journal and GPU-sensor access to $RUN_AS"
usermod -aG systemd-journal,video "$RUN_AS"

# Units the dashboard buttons may control, space separated. Deliberately empty
# by default: a wildcard rule here ("systemctl restart *") is matched by sudo
# across argument boundaries, so it would hand $RUN_AS root control of every
# unit on the host, sshd and tailscaled included.
#   sudo PIDASH_UNITS="jellyfin.service pihole.service" ./install.sh
PIDASH_UNITS="${PIDASH_UNITS:-}"

echo "==> Writing sudoers rules"
SYSTEMCTL="$(command -v systemctl)"
SS="$(command -v ss)"
{
  # Read-only, no wildcard: lets the Directory tab show which process owns each
  # listening socket. Without it you still get the ports, just not the names.
  echo "$RUN_AS ALL=(root) NOPASSWD: $SS -tulnpH"
  for unit in $PIDASH_UNITS; do
    [[ $unit =~ ^[A-Za-z0-9][A-Za-z0-9@._:-]*\.service$ ]] || {
      echo "Bad unit name: $unit" >&2; exit 1; }
    for verb in start stop restart; do
      echo "$RUN_AS ALL=(root) NOPASSWD: $SYSTEMCTL $verb $unit"
    done
  done
} > /etc/sudoers.d/pi-dash
chmod 440 /etc/sudoers.d/pi-dash
visudo -c -f /etc/sudoers.d/pi-dash >/dev/null || {
  rm -f /etc/sudoers.d/pi-dash
  echo "sudoers rule failed validation and was removed." >&2; exit 1; }

if [[ -z $PIDASH_UNITS ]]; then
  echo "    No PIDASH_UNITS set, so no systemctl rights were granted."
  echo "    The start/stop/restart buttons will error until you re-run with"
  echo "    PIDASH_UNITS=\"foo.service bar.service\"."
else
  echo "    Passwordless systemctl for: $PIDASH_UNITS"
fi

echo "==> Seeding the service directory"
CFG_HOME="$(getent passwd "$RUN_AS" | cut -d: -f6)/.config/pidash"
install -d -m 700 -o "$RUN_AS" -g "$RUN_AS" "$CFG_HOME"
if [[ ! -f $CFG_HOME/services.json ]]; then
  cp services.example.json "$CFG_HOME/services.json"
  chown "$RUN_AS:$RUN_AS" "$CFG_HOME/services.json"
  echo "    Wrote $CFG_HOME/services.json - edit it to match your services."
fi

echo "==> Installing service"
sed "s/User=%i/User=$RUN_AS/" pidash.service > /etc/systemd/system/pidash.service
systemctl daemon-reload
systemctl enable --now pidash

sleep 3
TOKEN_PATH="$(getent passwd "$RUN_AS" | cut -d: -f6)/.config/pidash/token"
echo
echo "  pi-dash is running."
echo "  url    http://$(tailscale ip -4 2>/dev/null | head -1):8088/"
echo "  token  sudo -u $RUN_AS cat $TOKEN_PATH"
echo
echo "  Group changes need a re-login or reboot before vcgencmd and the journal work."
