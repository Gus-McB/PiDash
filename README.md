# pi-dash

A single-host dashboard for a Raspberry Pi reachable over Tailscale. Six sections:

- **Panel** — CPU, core temp, memory, per-core load, disks, network, and the Pi 5 throttle flags
- **History** — the same measures recorded to disk and charted over hours or days, so you can answer questions about the time you were not watching
- **Processes** — what is actually using the CPU and the RAM right now, sorted and filterable
- **Units** — every systemd service, failures sorted to the top, with start/stop/restart
- **Faults** — journal errors across all units, filterable by priority, unit, window, and text
- **Directory** — health widgets for the services you care about, over a table of every listening socket

There is no shell in the browser. The dashboard reads state and can restart an explicit allowlist of units; it cannot run arbitrary commands. That is the whole security model, and it is why the token guards a read-mostly surface rather than a root prompt. The process table is part of that: it shows you what is running and deliberately offers no way to kill it.

Still worth doing: bind it to the tailnet only (the default, which refuses to guess if `tailscale0` is missing), and set a Tailscale ACL restricting port 8088 to your own devices rather than relying on the token alone.

## The History tab

The Panel's sparklines live in the browser and die on reload. The History tab is
the same measures written to a small SQLite file on the Pi, so the dashboard can
answer the question you actually reboot a Pi over: *was it throttling at 3am, or
is this new?*

A sampler writes one row every 30s — CPU, core temperature, memory, swap, load,
network rates, the fullest filesystem, and the live throttle bits. Rows are kept
for 14 days, which costs about 3 MB. Nothing is sampled through the same
counters `/api/metrics` uses; the recorder keeps its own deltas, because sharing
them would quietly shorten the interval of whoever is polling the Panel and make
the live readings wrong.

Reads are bucketed and averaged server-side, so a 7-day window and a 1-hour
window both return a few hundred points. Each chart shows the bucket average as
a line; CPU and temperature also shade up to the bucket **peak**, so a 40-second
spike is still visible in a window where each point covers half an hour.

- **Gaps are drawn as gaps.** If the Pi was off for five hours the line stops and
  restarts. It is never interpolated across, and hovering inside a gap reports
  nothing rather than inventing a sample.
- **Shaded columns** on the CPU and temperature charts mark samples where the SoC
  was throttled or under-volted. This is the reason the tab exists.
- **Hovering any chart crosshairs all of them** and shows one tooltip reading
  every metric at that instant.

Charts refresh on their own while the tab is open, but not while your pointer is
on them — the points would shift out from under the cursor.

## The Processes tab

The Panel tells you the load is 6.0 and the SoC is at 82 °C. This tab tells you
what is doing it.

Sorted by CPU or by resident memory, filterable by name, command line, user or
pid. The filter runs on the Pi against the full command line and before the
sort, so searching for `python` finds a process ranked 90th — but the table
shows only the first 200 characters of a command, and marks the cut with `…`
when a match came from further along.

CPU is reported the way `top` reports it, as a percentage of one core, so a busy
four-core Pi reads up to 400; the bar beside the number is scaled against all
cores. Values are deltas since the previous poll, which is 2.5s while you are
looking at the tab. The dashboard's own process usually sits near the top, for
the honest reason that it just did the work of building this table.

## The Directory tab

The point of this tab is to answer "what is running on this Pi, and who can reach it?" in one screen.

**Listening ports** is generated from `ss`, folding IPv4/IPv6 pairs and worker pools into one row each. The **Exposure** column is the part to read:

| Badge | Means |
|---|---|
| `LOOPBACK` | bound to `127.0.0.1` — only the Pi itself |
| `TAILNET` | bound to a Tailscale address — only your tailnet |
| `LAN` | bound to `0.0.0.0` or `::` — **every device on your local network** |
| `INTERFACE` | bound to one specific non-Tailscale address |

`LAN` is the only badge that takes a colour, because it is the only one that might surprise you. Ports that speak HTTP become links.

**Watched services** are probed from `~/.config/pidash/services.json`. Edit it and press Refresh — no restart. Copy `services.example.json` for the annotated version.

```json
{
  "services": [
    { "name": "Minecraft", "type": "minecraft", "host": "127.0.0.1", "port": 25565 },
    { "name": "Jellyfin",  "type": "jellyfin",  "url": "http://127.0.0.1:8096",
      "link": "http://your-pi:8096/" },
    { "name": "Proxy",     "type": "http", "url": "http://127.0.0.1:80/", "expect": "any" },
    { "name": "SSH",       "type": "tcp",  "host": "127.0.0.1", "port": 22 }
  ]
}
```

| `type` | Does |
|---|---|
| `minecraft` | Java-edition Server List Ping — MOTD, players online/max, version, server icon, latency |
| `jellyfin` | reads `/System/Info/Public`, which needs no API key |
| `http` | any URL; `expect` is a status code, or `"any"` when the service answers but needs auth (a 401 from a proxy still means it is up) |
| `tcp` | plain connect check — the fallback for anything else |

`link` is optional and only decides where the **Open** button points. Use a hostname your browser can resolve, not `127.0.0.1` — the link opens on your machine, not on the Pi.

Probes open real sockets, so they run on their own slow cadence: roughly every 15s while you are looking at the tab, every 60s otherwise to keep the rail's fault lamp honest.

## Install

Copy the folder to the Pi and run the installer:

```bash
scp -r pi-dash pi@your-pi:~/
ssh pi@your-pi
cd ~/pi-dash && sudo PIDASH_UNITS="jellyfin.service pihole.service" ./install.sh
```

It creates a venv in `/opt/pi-dash`, adds your user to `systemd-journal` and `video`, and enables the service. It prints the URL and the path to the token when it finishes.

`PIDASH_UNITS` is the list of units the start/stop/restart buttons may control. Each one gets its own literal sudoers line. Leave it unset and no sudoers rule is written at all — the dashboard still shows every unit, the buttons just return an error. Do not replace it with a wildcard: sudo matches `*` across argument boundaries, so `systemctl restart *` grants root control of every unit on the host, `sshd` and `tailscaled` included.

Group membership only takes effect after a re-login, so reboot once before expecting the throttle flags and journal to populate.

## Manual install

```bash
sudo mkdir -p /opt/pi-dash && sudo cp -r server.py static requirements.txt services.example.json /opt/pi-dash/
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
| `PIDASH_SERVICES` | `~/.config/pidash/services.json` | Widget definitions for the Directory tab. |
| `PIDASH_ALLOW_CONTROL` | `1` | Set to `0` to make the start/stop/restart buttons return 403. |
| `PIDASH_HISTORY` | `~/.config/pidash/history.db` | Sample store for the History tab. SQLite in WAL mode: `-wal` and `-shm` appear beside it while it writes and are checkpointed away when it closes. |
| `PIDASH_HISTORY_INTERVAL` | `30` | Seconds between samples. Minimum 5. |
| `PIDASH_HISTORY_DAYS` | `14` | Retention. About 3 MB at the default interval. Set to `0` to keep everything forever. |

Read the token with `cat ~/.config/pidash/token`. Rotate it with `rm ~/.config/pidash/token && sudo systemctl restart pidash`, then read the new one the same way. The token is deliberately never printed to stdout, because under systemd that means the journal, where anyone in `systemd-journal` can read it back — including through this dashboard's own Faults tab.

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

**Restart buttons fail** — the unit is not in `PIDASH_UNITS`. Check `/etc/sudoers.d/pi-dash` lists it, and verify with `sudo -n systemctl restart <unit>`. Re-run the installer with the unit added to grant it.

**Blank page or unstyled dashboard** — the fonts are vendored into `static/`, so this is no longer an internet problem. Check the browser console: the CSP is `default-src 'none'` with same-origin grants only, so anything you add that loads from another origin will be refused by design.

**Listening ports show no process names** — the `ss` sudoers line is missing. `ss` only reveals other users' processes to root. Re-run the installer, or add it by hand: `pidash ALL=(root) NOPASSWD: /usr/bin/ss -tulnpH`. The ports themselves are listed either way; you only lose the names.

**History says it is not recording** — the tab prints the exact error from the
sample store. It is almost always that the service user cannot write to
`~/.config/pidash/`. The rest of the dashboard is unaffected; only this tab goes
dark.

**History is empty after an install** — nothing is backfilled, because nothing
was recorded before the service started. The first point appears one interval
after boot, and a 7-day window is only useful after 7 days.

**A widget says Down but the service works** — check what you are probing. An HTTP service behind auth returns 401, which counts as down unless you set `"expect": "any"`. A `link` is never probed; only `url`/`host`+`port` are.

## What the throttle lamps mean

They decode `vcgencmd get_throttled`, which is the thing that actually bites a Pi in a sealed rack. Top row is current state, bottom row is whether it has happened since boot.

- **Under-voltage** — the supply is sagging below 4.63 V. Almost always the PSU or the cable, not the load.
- **Freq capped** — the ARM clock has been capped.
- **Throttled** — the SoC is actively throttling, usually at 80 to 85 °C.
- **Soft temp limit** — the 60 °C soft limit on Pi 5 has kicked in and is reducing clocks early.

A bottom-row lamp lit amber with the top row dark means it happened earlier and has since recovered. Worth chasing before it becomes a habit.
