#!/usr/bin/env bash
# Put the dashboard on the attached HDMI panel at login.
# Run as the desktop user, without sudo: everything it writes is that user's.
#   ./kiosk/install-kiosk.sh
set -euo pipefail

DEST=/opt/pi-dash/kiosk
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pidash"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] && { echo "Run this as the desktop user, not with sudo."; exit 1; }

command -v chromium >/dev/null || {
  echo "chromium is not installed: sudo apt install -y chromium"; exit 1; }
command -v wlr-randr >/dev/null || \
  echo "    wlr-randr is missing, so rotation will be skipped: sudo apt install -y wlr-randr"

echo "==> Installing the launcher to $DEST"
if [[ -w /opt/pi-dash ]]; then
  install -d "$DEST"
else
  sudo install -d -o "$USER" -g "$USER" "$DEST"
fi
install -m 755 "$SRC/pidash-kiosk.sh" "$DEST/pidash-kiosk.sh"

echo "==> Seeding $CFG_DIR/kiosk.conf"
install -d -m 700 "$CFG_DIR"
if [[ -f $CFG_DIR/kiosk.conf ]]; then
  echo "    Already there, left alone."
else
  install -m 600 "$SRC/kiosk.example.conf" "$CFG_DIR/kiosk.conf"
fi

echo "==> Adding the autostart entry"
install -d "$AUTOSTART"
install -m 644 "$SRC/pidash-kiosk.desktop" "$AUTOSTART/pidash-kiosk.desktop"

echo
echo "  Installed. It starts at the next login."
echo "  Try it now without logging out:  $DEST/pidash-kiosk.sh"
echo "  Panel too small or sideways:     edit $CFG_DIR/kiosk.conf"
echo "  Turn it off:                     rm $AUTOSTART/pidash-kiosk.desktop"
