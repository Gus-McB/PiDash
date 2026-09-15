#!/usr/bin/env bash
# Launch the dashboard full screen on the attached HDMI panel.
# Started by ~/.config/autostart/pidash-kiosk.desktop inside the labwc session.
set -euo pipefail

CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pidash"
CONF="$CFG_DIR/kiosk.conf"
TOKEN_FILE="$CFG_DIR/token"
PROFILE="${XDG_DATA_HOME:-$HOME/.local/share}/pidash-kiosk"

# Defaults, overridden by kiosk.conf.
PIDASH_KIOSK_URL=""
PIDASH_KIOSK_SCALE="1.0"
PIDASH_KIOSK_OUTPUT=""
PIDASH_KIOSK_ROTATE=""
PIDASH_KIOSK_WAIT="120"

# shellcheck source=/dev/null
[[ -f $CONF ]] && . "$CONF"

log() { printf 'pidash-kiosk: %s\n' "$*" >&2; }

# The service binds to the tailscale0 address unless told otherwise, and the Pi
# can reach its own tailnet address, so that is the default target. Loopback is
# the fallback for a host where PIDASH_BIND was set to 127.0.0.1 or 0.0.0.0.
# Resolved on every attempt, not once: at login tailscaled often has no address
# yet, and a URL fixed to loopback then would never come right.
resolve_url() {
  if [[ -n $PIDASH_KIOSK_URL ]]; then URL="$PIDASH_KIOSK_URL"; return; fi
  local ip
  ip="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  URL="http://${ip:-127.0.0.1}:8088/"
}

# Rotation and mode are the panel's business, not the browser's. Only touched
# when asked, so an autodetected mode is left exactly as the compositor found it.
if [[ -n $PIDASH_KIOSK_OUTPUT && -n $PIDASH_KIOSK_ROTATE ]]; then
  wlr-randr --output "$PIDASH_KIOSK_OUTPUT" --transform "$PIDASH_KIOSK_ROTATE" \
    || log "wlr-randr failed; leaving the output as it is"
fi

# tailscaled may still be handing out an address when the session starts, and a
# kiosk that boots to a connection error stays on a connection error until
# somebody walks over to it. Wait for the dashboard to actually answer.
deadline=$(( SECONDS + PIDASH_KIOSK_WAIT ))
while resolve_url; ! curl -sf -o /dev/null --max-time 2 "$URL"; do
  if (( SECONDS >= deadline )); then
    log "no answer from $URL after ${PIDASH_KIOSK_WAIT}s; starting anyway"
    break
  fi
  sleep 2
done

# The panel has no keyboard, so the token has to arrive with the URL. It is
# never passed as an argument: argv is world readable through /proc, and this
# dashboard's own Processes tab prints command lines. A 0600 bootstrap page
# inside the 0700 config directory hands it over in a fragment, which the
# browser keeps to itself, and index.html clears the fragment on arrival.
START_PAGE="$CFG_DIR/kiosk-start.html"
if [[ -r $TOKEN_FILE ]]; then
  ( umask 077
    { printf '<!doctype html><meta charset="utf-8"><title>pi-dash</title><script>\n'
      printf 'location.replace(%s + "#token=" + %s);\n' \
        "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$URL")" \
        "$(python3 -c 'import json,sys,urllib.parse; print(json.dumps(urllib.parse.quote(sys.argv[1], safe="")))' "$(cat "$TOKEN_FILE")")"
      printf '</script>\n'
    } > "$START_PAGE"
  )
  TARGET="file://$START_PAGE"
else
  log "no token at $TOKEN_FILE; the gate will ask for one"
  TARGET="$URL"
fi

# A kiosk that was cut off at the mains must not come back asking to restore
# tabs, and must never offer an update prompt nobody is there to dismiss.
exec chromium \
  --ozone-platform=wayland \
  --kiosk \
  --user-data-dir="$PROFILE" \
  --force-device-scale-factor="$PIDASH_KIOSK_SCALE" \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --hide-crash-restore-bubble \
  --disable-session-crashed-bubble \
  --disable-pinch \
  --password-store=basic \
  --check-for-update-interval=31536000 \
  --disable-features=Translate,AutofillServerCommunication \
  "$TARGET"
