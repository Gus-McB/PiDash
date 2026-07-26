# pi-dash

A single-host dashboard for a Raspberry Pi reachable over Tailscale. Four sections:

- **Panel** — CPU, core temp, memory, per-core load, disks, network, and the Pi 5 throttle flags
- **Units** — every systemd service, failures sorted to the top, with start/stop/restart
- **Faults** — journal errors across all units, filterable by priority, unit, window, and text
- **Shell** — a real pty in the browser, with job control and resize

## Read this before installing

The Shell tab is a full login shell as the service user. Anyone who has the token has that shell. Two consequences:

- Bind it to the tailnet only. The default does this automatically, and refuses to guess if `tailscale0` is missing.
- Lock the tailnet down. The token is the only thing between a device on your tailnet and a shell, so set a Tailscale ACL restricting port 8088 to your own devices rather than relying on the token alone.

If you would rather not expose a shell at all, delete the `ws_term` handler from `server.py` and the Shell slot from `index.html`. Everything else works without it.

## Install

Copy the folder to the Pi and run the installer:

```bash
scp -r pi-dash pi@your-pi:~/
ssh pi@your-pi
cd ~/pi-dash && sudo ./install.sh
```

It creates a venv in `/opt/pi-dash`, adds your user to `systemd-journal` and `video`, writes a narrow sudoers rule for `systemctl`, and enables the service. It prints the URL and token when it finishes.

Group membership only takes effect after a re-login, so reboot once before expecting the throttle flags and journal to populate.

## Manual install

```bash
sudo mkdir -p /opt/pi-dash && sudo cp -r server.py static requirements.txt /opt/pi-dash/
cd /opt/pi-dash
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo chown -R $USER:$USER /opt/pi-dash
sudo usermod -aG systemd-journal,video $USER
sudo sed "s/User=%i/User=$USER/" ~/pi-dash/pidash.service > /etc/systemd/system/pidash.service
sudo systemctl enable --now pidash
```

## Configuration

Set these in the `[Service]` block of `/etc/systemd/system/pidash.service`.

| Variable | Default | Notes |
|---|---|---|
| `PIDASH_BIND` | tailscale0 address | Falls back to `127.0.0.1` if Tailscale is not up. `0.0.0.0` exposes it to your LAN. |
| `PIDASH_PORT` | `8088` | |
| `PIDASH_TOKEN` | generated | Otherwise read from or written to `~/.config/pidash/token`. |
| `PIDASH_SHELL` | `$SHELL` | The shell spawned by the Shell tab. |
| `PIDASH_ALLOW_CONTROL` | `1` | Set to `0` to make the start/stop/restart buttons return 403. |

Rotate the token with `rm ~/.config/pidash/token && sudo systemctl restart pidash`, then read the new one from `journalctl -u pidash -n 20`.

## HTTPS

Plain HTTP inside a tailnet is already encrypted by WireGuard, but browsers treat it as insecure. To get a real certificate:

```bash
sudo tailscale cert "$(tailscale status --json | jq -r .Self.DNSName | sed 's/\.$//')"
sudo tailscale serve --bg --https 443 http://127.0.0.1:8088
```

Then set `PIDASH_BIND=127.0.0.1` so only the Tailscale proxy can reach the app, and use `https://your-pi.your-tailnet.ts.net/`. WebSockets pass through `tailscale serve` unchanged.

## Troubleshooting

**Throttle lamps say vcgencmd unavailable** — the service user is not in the `video` group, or you have not re-logged in since being added. Check with `groups` and `vcgencmd get_throttled`.

**Faults tab says the journal is unreadable** — the user is not in `systemd-journal`. Without it, `journalctl` only shows that user's own messages.

**Restart buttons fail** — the sudoers rule is missing or the unit name did not match. Verify with `sudo -n systemctl restart ssh`.

**Blank page or unstyled dashboard** — the fonts and xterm.js load from jsdelivr, so the browser needs internet even though the Pi does not. To vendor them, download `xterm.js`, `addon-fit.js`, and `xterm.css` into `static/` and change the three CDN tags in `index.html` to `/static/...`.

**Terminal connects then immediately closes** — the shell in `PIDASH_SHELL` is missing or the user has `/usr/sbin/nologin`. Check `getent passwd $USER`.

## What the throttle lamps mean

They decode `vcgencmd get_throttled`, which is the thing that actually bites a Pi in a sealed rack. Top row is current state, bottom row is whether it has happened since boot.

- **Under-voltage** — the supply is sagging below 4.63 V. Almost always the PSU or the cable, not the load.
- **Freq capped** — the ARM clock has been capped.
- **Throttled** — the SoC is actively throttling, usually at 80 to 85 °C.
- **Soft temp limit** — the 60 °C soft limit on Pi 5 has kicked in and is reducing clocks early.

A bottom-row lamp lit amber with the top row dark means it happened earlier and has since recovered. Worth chasing before it becomes a habit.
