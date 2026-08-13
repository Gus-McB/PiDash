#!/usr/bin/env python3
"""
pi-dash: a single-host dashboard for a Raspberry Pi on a tailnet.

Serves metrics, systemd unit state, journal errors, a directory of every
listening socket, and health probes for the services running on the host.
Intended to be bound to the Tailscale interface only. See README.md.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, closing
from hmac import compare_digest
from pathlib import Path
from typing import Any

import psutil
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

CONFIG_DIR = Path(os.environ.get("PIDASH_CONFIG", Path.home() / ".config" / "pidash"))
TOKEN_FILE = CONFIG_DIR / "token"
SERVICES_FILE = Path(os.environ.get("PIDASH_SERVICES", CONFIG_DIR / "services.json"))

PORT = int(os.environ.get("PIDASH_PORT", "8088"))
BIND = os.environ.get("PIDASH_BIND", "")  # blank means auto-detect tailscale0
ALLOW_CONTROL = os.environ.get("PIDASH_ALLOW_CONTROL", "1") == "1"

HISTORY_FILE = Path(os.environ.get("PIDASH_HISTORY", CONFIG_DIR / "history.db"))
HISTORY_INTERVAL = max(5, int(os.environ.get("PIDASH_HISTORY_INTERVAL", "30")))
HISTORY_DAYS = max(0, int(os.environ.get("PIDASH_HISTORY_DAYS", "14")))

# ---------------------------------------------------------------- auth


def load_token() -> str:
    env = os.environ.get("PIDASH_TOKEN")
    if env:
        return env.strip()
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tok = secrets.token_urlsafe(32)
    # Create with 0600 in place rather than writing then chmod'ing, so the token
    # is never briefly readable at the umask default.
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok + "\n")
    return tok


TOKEN = load_token()
TOKEN_BYTES = TOKEN.encode()


def check_token(supplied: str | None) -> bool:
    if not supplied:
        return False
    # compare_digest raises TypeError on non-ASCII str, and query params are
    # UTF-8 decoded, so compare as bytes to keep a hostile token a plain 401.
    return compare_digest(supplied.encode(), TOKEN_BYTES)


async def require_auth(request: Request) -> None:
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else None
    if not check_token(supplied):
        raise HTTPException(status_code=401, detail="Bad or missing token")


# ---------------------------------------------------------------- helpers


def run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out"


def read_first(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def tailscale_ip() -> str | None:
    for addrs in psutil.net_if_addrs().get("tailscale0", []):
        if addrs.family.name == "AF_INET":
            return addrs.address
    code, out, _ = run(["tailscale", "ip", "-4"], timeout=3)
    if code == 0 and out.strip():
        return out.strip().splitlines()[0]
    return None


def pi_model() -> str:
    raw = read_first("/proc/device-tree/model") or read_first("/sys/firmware/devicetree/base/model")
    if raw:
        return raw.replace("\x00", "").strip()
    return "Unknown host"


# ---------------------------------------------------------------- thermal


def cpu_temp_c() -> float | None:
    raw = read_first("/sys/class/thermal/thermal_zone0/temp")
    if raw and raw.lstrip("-").isdigit():
        return round(int(raw) / 1000.0, 1)
    try:
        for entries in psutil.sensors_temperatures().values():
            for e in entries:
                if e.current:
                    return round(e.current, 1)
    except Exception:
        pass
    return None


def thermal_zones() -> dict[str, float]:
    zones: dict[str, float] = {}
    base = Path("/sys/class/thermal")
    if not base.exists():
        return zones
    for zone in sorted(base.glob("thermal_zone*")):
        label = read_first(str(zone / "type")) or zone.name
        raw = read_first(str(zone / "temp"))
        if raw and raw.lstrip("-").isdigit():
            zones[label] = round(int(raw) / 1000.0, 1)
    return zones


def fan_rpm() -> int | None:
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        raw = read_first(str(hwmon / "fan1_input"))
        if raw and raw.isdigit():
            return int(raw)
    return None


THROTTLE_BITS = [
    (0, "undervolt", "Under-voltage", "now"),
    (1, "freqcap", "Freq capped", "now"),
    (2, "throttled", "Throttled", "now"),
    (3, "softtemp", "Soft temp limit", "now"),
    (16, "undervolt_ever", "Under-voltage", "boot"),
    (17, "freqcap_ever", "Freq capped", "boot"),
    (18, "throttled_ever", "Throttled", "boot"),
    (19, "softtemp_ever", "Soft temp limit", "boot"),
]

_VCGENCMD = shutil.which("vcgencmd")


def throttle_state() -> dict[str, Any]:
    flags = [
        {"key": key, "label": label, "scope": scope, "set": False}
        for _, key, label, scope in THROTTLE_BITS
    ]
    if not _VCGENCMD:
        return {"available": False, "raw": None, "flags": flags}
    code, out, _ = run([_VCGENCMD, "get_throttled"], timeout=3)
    m = re.search(r"0x([0-9a-fA-F]+)", out or "")
    if code != 0 or not m:
        return {"available": False, "raw": None, "flags": flags}
    value = int(m.group(1), 16)
    for flag, (bit, _k, _l, _s) in zip(flags, THROTTLE_BITS):
        flag["set"] = bool(value & (1 << bit))
    return {"available": True, "raw": f"0x{value:x}", "flags": flags}


def core_volts() -> float | None:
    if not _VCGENCMD:
        return None
    code, out, _ = run([_VCGENCMD, "measure_volts", "core"], timeout=3)
    m = re.search(r"([0-9.]+)V", out or "")
    return round(float(m.group(1)), 4) if code == 0 and m else None


# ---------------------------------------------------------------- metrics

_net_prev: dict[str, float] = {}


def net_rates() -> dict[str, float]:
    global _net_prev
    io = psutil.net_io_counters()
    now = time.time()
    rates = {"rx_bps": 0.0, "tx_bps": 0.0}
    if _net_prev:
        dt = now - _net_prev["t"]
        if dt > 0:
            rates["rx_bps"] = max(0.0, (io.bytes_recv - _net_prev["rx"]) / dt)
            rates["tx_bps"] = max(0.0, (io.bytes_sent - _net_prev["tx"]) / dt)
    _net_prev = {"t": now, "rx": io.bytes_recv, "tx": io.bytes_sent}
    rates["rx_total"] = io.bytes_recv
    rates["tx_total"] = io.bytes_sent
    return rates


def disks() -> list[dict[str, Any]]:
    out = []
    seen = set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("squashfs", "overlay", "tmpfs", "devtmpfs"):
            continue
        if part.device in seen:
            continue
        seen.add(part.device)
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        out.append(
            {
                "mount": part.mountpoint,
                "device": part.device,
                "fstype": part.fstype,
                "total": u.total,
                "used": u.used,
                "percent": u.percent,
            }
        )
    return out


def service_counts() -> dict[str, int]:
    units = list_units()
    return {
        "total": len(units),
        "running": sum(1 for u in units if u["sub"] == "running"),
        "failed": sum(1 for u in units if u["active"] == "failed"),
    }


def collect_metrics() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    freq = psutil.cpu_freq()
    load = os.getloadavg()
    return {
        "ts": time.time(),
        "host": {
            "hostname": os.uname().nodename,
            "model": pi_model(),
            "kernel": os.uname().release,
            "uptime_s": int(time.time() - psutil.boot_time()),
            "tailscale_ip": tailscale_ip(),
            "load": [round(x, 2) for x in load],
        },
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "per_core": psutil.cpu_percent(interval=None, percpu=True),
            "count": psutil.cpu_count(),
            "freq_mhz": round(freq.current) if freq else None,
            "freq_max_mhz": round(freq.max) if freq and freq.max else None,
        },
        "mem": {
            "total": vm.total,
            "used": vm.total - vm.available,
            "available": vm.available,
            "percent": vm.percent,
            "swap_total": sm.total,
            "swap_used": sm.used,
            "swap_percent": sm.percent,
        },
        "disks": disks(),
        "net": net_rates(),
        "thermal": {
            "cpu_c": cpu_temp_c(),
            "zones": thermal_zones(),
            "fan_rpm": fan_rpm(),
            "core_volts": core_volts(),
        },
        "throttle": throttle_state(),
        "services": service_counts(),
    }


# ---------------------------------------------------------------- processes

PROC_ATTRS = [
    "pid",
    "name",
    "username",
    "cpu_percent",
    "memory_percent",
    "memory_info",
    "create_time",
    "status",
    "cmdline",
    "num_threads",
]

# The filter matches the whole command line but the table shows a prefix, so a
# row can match on an argument that is off the end. Mark the cut rather than
# leaving the match looking arbitrary.
CMD_MAX = 200

# psutil derives cpu_percent from the delta since the last call on the same
# Process object, and process_iter caches those objects between calls. That is
# what makes the numbers meaningful, and also what makes a first call — or one
# after a long gap — an average over a uselessly wide window. Re-prime instead.
_proc_last_read = 0.0
PROC_PRIME_AFTER = 15.0
PROC_PRIME_WINDOW = 0.3


def _prime_cpu_percent() -> None:
    for proc in psutil.process_iter():
        try:
            proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def top_processes(limit: int = 25, sort: str = "cpu", query: str = "") -> dict[str, Any]:
    global _proc_last_read
    now = time.time()
    if now - _proc_last_read > PROC_PRIME_AFTER:
        _prime_cpu_percent()
        time.sleep(PROC_PRIME_WINDOW)
    _proc_last_read = time.time()

    q = query.strip().lower()
    total = 0
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(PROC_ATTRS):
        info = proc.info
        total += 1
        argv = info.get("cmdline") or []
        # Kernel threads have no argv at all; show the bracketed name journald
        # and ps use for them rather than an empty cell.
        cmd = " ".join(argv) if argv else f"[{info.get('name') or '?'}]"
        name = info.get("name") or "?"
        user = info.get("username") or "—"
        if q and q not in name.lower() and q not in cmd.lower() \
                and q not in user.lower() and q != str(info["pid"]):
            continue
        mem = info.get("memory_info")
        rows.append(
            {
                "pid": info["pid"],
                "name": name,
                "cmd": cmd if len(cmd) <= CMD_MAX else cmd[: CMD_MAX - 1] + "…",
                "user": user,
                "cpu": round(info.get("cpu_percent") or 0.0, 1),
                "mem_percent": round(info.get("memory_percent") or 0.0, 2),
                "rss": mem.rss if mem else 0,
                "threads": info.get("num_threads") or 0,
                "status": info.get("status") or "?",
                "started": info.get("create_time"),
                "kernel": not argv,
            }
        )

    key = (lambda r: (r["rss"], r["cpu"])) if sort == "mem" else (lambda r: (r["cpu"], r["rss"]))
    rows.sort(key=key, reverse=True)
    return {
        "processes": rows[:limit],
        "matched": len(rows),
        "total": total,
        "cores": psutil.cpu_count() or 1,
    }


# ---------------------------------------------------------------- history


HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts       INTEGER PRIMARY KEY,
    cpu      REAL,
    temp     REAL,
    mem      REAL,
    swap     REAL,
    load1    REAL,
    rx       REAL,
    tx       REAL,
    disk     REAL,
    throttle INTEGER
);
"""

# Which throttle bits count as "the SoC was actually being held back". The
# since-boot bits (16..19) are latches, not events, so they are deliberately
# excluded — a sample is only marked throttled if it was throttling right then.
THROTTLE_NOW_MASK = 0b1111


class History:
    """A small on-disk time series, so the dashboard can answer questions about
    the hours you were not watching.

    It keeps its own counter deltas rather than calling net_rates() or
    psutil.cpu_percent(). Both of those hold module-level state that belongs to
    whoever is polling /api/metrics; sampling through them would silently
    shorten that caller's interval and corrupt the live readings.
    """

    def __init__(self, path: Path, retain_days: int) -> None:
        self.path = path
        self.retain_days = retain_days
        self.enabled = False
        self.reason = "not started"
        self._prev: dict[str, Any] | None = None
        self._writes = 0

    # -- lifecycle ------------------------------------------------------

    def open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with closing(self._connect()) as db:
                db.executescript(HISTORY_SCHEMA)
                db.commit()
            self.enabled = True
            self.reason = ""
        except (OSError, sqlite3.Error) as exc:
            self.enabled = False
            self.reason = f"{type(exc).__name__}: {exc}"

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0)
        # WAL survives an unclean shutdown without a fsync on every sample,
        # which matters when the store is an SD card.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    # -- writing --------------------------------------------------------

    def _read_counters(self) -> dict[str, Any]:
        times = psutil.cpu_times()
        net = psutil.net_io_counters()
        return {
            "t": time.time(),
            "cpu_total": sum(times),
            "cpu_idle": times.idle + getattr(times, "iowait", 0.0),
            "rx": net.bytes_recv,
            "tx": net.bytes_sent,
        }

    def sample(self) -> bool:
        """Take one sample. Returns False when it only primed the deltas."""
        cur = self._read_counters()
        prev, self._prev = self._prev, cur
        if prev is None:
            return False
        dt = cur["t"] - prev["t"]
        d_total = cur["cpu_total"] - prev["cpu_total"]
        d_idle = cur["cpu_idle"] - prev["cpu_idle"]
        if dt <= 0 or d_total <= 0:
            return False

        cpu = max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total)))
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        mounts = disks()
        thr = throttle_state()
        bits = int(thr["raw"], 16) if thr["available"] and thr["raw"] else 0

        row = (
            int(cur["t"]),
            round(cpu, 2),
            cpu_temp_c(),
            round(vm.percent, 2),
            round(sm.percent, 2),
            round(os.getloadavg()[0], 2),
            round(max(0.0, (cur["rx"] - prev["rx"]) / dt), 1),
            round(max(0.0, (cur["tx"] - prev["tx"]) / dt), 1),
            round(max((d["percent"] for d in mounts), default=0.0), 2),
            bits & THROTTLE_NOW_MASK,
        )
        with closing(self._connect()) as db:
            db.execute(
                "INSERT OR REPLACE INTO samples "
                "(ts,cpu,temp,mem,swap,load1,rx,tx,disk,throttle) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            self._writes += 1
            # Pruning every write would be a delete scan every 30s for nothing;
            # once an hour of samples is often enough to hold the retention line.
            if self.retain_days and self._writes % max(1, int(3600 / HISTORY_INTERVAL)) == 1:
                cutoff = int(cur["t"] - self.retain_days * 86400)
                db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            db.commit()
        return True

    # -- reading --------------------------------------------------------

    def series(self, span_s: int, points: int) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": self.enabled,
            "reason": self.reason,
            "span_s": span_s,
            "interval_s": HISTORY_INTERVAL,
            "retain_days": self.retain_days,
        }
        if not self.enabled:
            out.update({"count": 0, "ts": [], "oldest": None})
            return out

        now = int(time.time())
        start = now - span_s
        # One bucket must hold at least one sample, or the chart grows comb
        # teeth where the bucket grid and the sample grid disagree.
        width = max(HISTORY_INTERVAL, span_s // max(1, points))
        try:
            with closing(self._connect()) as db:
                oldest = db.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
                rows = db.execute(
                    "SELECT (ts/?)*? AS bucket,"
                    "       AVG(cpu), MAX(cpu), AVG(temp), MAX(temp),"
                    "       AVG(mem), AVG(swap), MAX(load1),"
                    "       AVG(rx), AVG(tx), MAX(disk), MAX(throttle), COUNT(*)"
                    " FROM samples WHERE ts >= ?"
                    " GROUP BY bucket ORDER BY bucket",
                    (width, width, start),
                ).fetchall()
        except sqlite3.Error as exc:
            out.update({"enabled": False, "reason": str(exc), "count": 0, "ts": [], "oldest": None})
            return out

        rnd = lambda v, n: None if v is None else round(v, n)  # noqa: E731
        out.update(
            {
                "bucket_s": width,
                "count": len(rows),
                "oldest": oldest,
                # The window comes from the server so the charts do not skew
                # when the browser's clock disagrees with the Pi's.
                "start": start,
                "end": now,
                "ts": [r[0] for r in rows],
                "cpu": [rnd(r[1], 1) for r in rows],
                "cpu_max": [rnd(r[2], 1) for r in rows],
                "temp": [rnd(r[3], 1) for r in rows],
                "temp_max": [rnd(r[4], 1) for r in rows],
                "mem": [rnd(r[5], 1) for r in rows],
                "swap": [rnd(r[6], 1) for r in rows],
                "load1": [rnd(r[7], 2) for r in rows],
                "rx": [rnd(r[8], 0) for r in rows],
                "tx": [rnd(r[9], 0) for r in rows],
                "disk": [rnd(r[10], 1) for r in rows],
                "throttle": [r[11] or 0 for r in rows],
                "samples": [r[12] for r in rows],
            }
        )
        return out


HISTORY = History(HISTORY_FILE, HISTORY_DAYS)


async def history_loop() -> None:
    await asyncio.to_thread(HISTORY.sample)  # prime the deltas, write nothing
    while True:
        await asyncio.sleep(HISTORY_INTERVAL)
        try:
            await asyncio.to_thread(HISTORY.sample)
        except Exception as exc:  # a broken sampler must never take the app down
            print(f"!! history sample failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- systemd


def list_units() -> list[dict[str, Any]]:
    code, out, _ = run(
        ["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--output=json"]
    )
    if code == 0 and out.strip():
        try:
            raw = json.loads(out)
            return [
                {
                    "unit": u.get("unit", ""),
                    "load": u.get("load", ""),
                    "active": u.get("active", ""),
                    "sub": u.get("sub", ""),
                    "description": u.get("description", ""),
                }
                for u in raw
            ]
        except json.JSONDecodeError:
            pass
    # Fallback for older systemd without --output=json
    code, out, _ = run(
        ["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--plain",
         "--no-legend"]
    )
    units = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4 and parts[0].endswith(".service"):
            units.append(
                {
                    "unit": parts[0],
                    "load": parts[1],
                    "active": parts[2],
                    "sub": parts[3],
                    "description": parts[4] if len(parts) > 4 else "",
                }
            )
    return units


# Must open with an alphanumeric: a leading "-" would reach systemctl as an
# option rather than a unit name.
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:\\-]*\.service$")


# ---------------------------------------------------------------- port directory

# Only used to give an unnamed socket a human label when we cannot see the
# owning process. Not authoritative — the process name always wins.
WELL_KNOWN = {
    22: "ssh", 53: "dns", 80: "http", 111: "rpcbind", 123: "ntp", 143: "imap",
    443: "https", 445: "smb", 631: "cups", 3000: "grafana", 3306: "mysql",
    5000: "upnp", 5353: "mdns", 5432: "postgres", 6379: "redis", 8006: "proxmox",
    8080: "http-alt", 8096: "jellyfin", 8123: "home-assistant", 8443: "https-alt",
    9090: "prometheus", 19132: "minecraft-be", 25565: "minecraft", 32400: "plex",
    51820: "wireguard",
}

# Ports we will offer as a clickable link in the directory.
HTTP_PORTS = {80, 81, 443, 3000, 8006, 8080, 8081, 8088, 8096, 8123, 8443, 9090, 32400}

_SS_RE = re.compile(
    r"^(?P<proto>\S+)\s+\S+\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+(?:\s+(?P<rest>.*))?$"
)
_SS_PROC_RE = re.compile(r'"(?P<name>[^"]+)",pid=(?P<pid>\d+)')


def _split_hostport(addr: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    return host.strip("[]") or "*", int(port) if port.isdigit() else 0


def classify_bind(host: str) -> str:
    """How far a listening socket actually reaches. The point of the directory."""
    if host in ("*", "0.0.0.0", "::", ""):
        return "lan"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "iface"
    if ip.is_loopback:
        return "local"
    # Tailscale hands out 100.64.0.0/10 (CGNAT) and fd7a:115c:a1e0::/48.
    if ip in ipaddress.ip_network("100.64.0.0/10") or str(ip).startswith("fd7a:115c:a1e0"):
        return "tailnet"
    return "iface"


def listening_sockets() -> dict[str, Any]:
    """Every listening TCP/UDP socket, with the owning process where visible.

    `ss` only reveals other users' process names to root, so we try sudo first
    and fall back to an unprivileged listing. Ports are always visible either
    way; without the sudoers line you lose the names, not the directory.
    """
    named = True
    code, out, _ = run(["sudo", "-n", "ss", "-tulnpH"], timeout=8)
    if code != 0:
        named = False
        code, out, _ = run(["ss", "-tulnpH"], timeout=8)
        if code != 0:
            return {"sockets": [], "named": False, "error": "ss unavailable"}

    seen: dict[tuple, dict] = {}
    for line in out.splitlines():
        m = _SS_RE.match(line.strip())
        if not m:
            continue
        proto = m.group("proto").lower()
        host, port = _split_hostport(m.group("local"))
        if not port:
            continue
        procs = _SS_PROC_RE.findall(m.group("rest") or "")
        name = procs[0][0] if procs else None
        pids = sorted({int(p) for _n, p in procs})
        key = (proto, port, classify_bind(host))
        entry = seen.get(key)
        if entry:
            # Same service on v4 and v6, or an nginx worker pool: fold together.
            entry["pids"] = sorted(set(entry["pids"]) | set(pids))
            continue
        seen[key] = {
            "proto": proto,
            "port": port,
            "host": host,
            "scope": classify_bind(host),
            "process": name,
            "pids": pids,
            "guess": WELL_KNOWN.get(port),
        }

    sockets = sorted(seen.values(), key=lambda s: (s["port"], s["proto"]))
    for s in sockets:
        s["label"] = s["process"] or s["guess"] or "unknown"
        # Anything speaking HTTP on a non-loopback bind is worth a click.
        s["http"] = s["proto"].startswith("tcp") and s["port"] in HTTP_PORTS
    return {"sockets": sockets, "named": named and any(s["process"] for s in sockets)}


# ---------------------------------------------------------------- journal

PRIORITY_NAMES = {
    0: "emerg", 1: "alert", 2: "crit", 3: "err",
    4: "warning", 5: "notice", 6: "info", 7: "debug",
}


def decode_message(msg: Any) -> str:
    if isinstance(msg, list):
        try:
            return bytes(msg).decode("utf-8", "replace")
        except Exception:
            return str(msg)
    return str(msg or "")


def read_journal(priority: int, lines: int, unit: str | None, since: str | None) -> list[dict]:
    cmd = ["journalctl", "-o", "json", "--no-pager", "-n", str(lines), "-p", str(priority)]
    if unit and UNIT_RE.match(unit):
        cmd += ["-u", unit]
    if since:
        cmd += ["--since", since]
    code, out, err = run(cmd, timeout=25)
    if code != 0:
        return [
            {
                "ts": time.time(),
                "priority": 3,
                "priority_name": "err",
                "unit": "pi-dash",
                "message": f"journalctl failed: {err.strip() or code}",
            }
        ]
    entries = []
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            ts = int(e.get("__REALTIME_TIMESTAMP", "0")) / 1_000_000
        except (TypeError, ValueError):
            ts = 0.0
        prio = int(e.get("PRIORITY", 6) or 6)
        entries.append(
            {
                "ts": ts,
                "priority": prio,
                "priority_name": PRIORITY_NAMES.get(prio, str(prio)),
                "unit": e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER") or "kernel",
                "pid": e.get("_PID"),
                "message": decode_message(e.get("MESSAGE")),
            }
        )
    entries.reverse()
    return entries


# ---------------------------------------------------------------- service probes


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock: socket.socket) -> int:
    n = shift = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise OSError("connection closed mid-varint")
        n |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return n
        shift += 7
        if shift > 35:
            raise OSError("varint too long")


MOTD_CODES = re.compile(r"\xa7[0-9a-fk-or]", re.IGNORECASE)


def flatten_motd(desc: Any) -> str:
    """MOTD is either a plain string or a nested chat component."""
    if isinstance(desc, str):
        return MOTD_CODES.sub("", desc)
    if isinstance(desc, dict):
        text = desc.get("text", "")
        for child in desc.get("extra", []) or []:
            text += flatten_motd(child)
        return MOTD_CODES.sub("", text)
    if isinstance(desc, list):
        return "".join(flatten_motd(d) for d in desc)
    return ""


def probe_minecraft(host: str, port: int, timeout: float) -> dict[str, Any]:
    """Server List Ping — the same handshake the vanilla client's server list uses."""
    t0 = time.monotonic()
    with socket.create_connection((host, port), timeout) as s:
        s.settimeout(timeout)
        addr = host.encode()
        hs = (
            b"\x00" + _varint(767) + _varint(len(addr)) + addr
            + struct.pack(">H", port) + _varint(1)
        )
        s.sendall(_varint(len(hs)) + hs)
        s.sendall(_varint(1) + b"\x00")  # status request
        _read_varint(s)  # packet length
        if _read_varint(s) != 0x00:
            raise OSError("unexpected packet id")
        n = _read_varint(s)
        buf = b""
        while len(buf) < n:
            chunk = s.recv(min(8192, n - len(buf)))
            if not chunk:
                raise OSError("connection closed mid-payload")
            buf += chunk
    data = json.loads(buf.decode("utf-8", "replace"))
    players = data.get("players") or {}
    sample = [p.get("name", "") for p in (players.get("sample") or []) if p.get("name")]
    return {
        "up": True,
        "latency_ms": round((time.monotonic() - t0) * 1000),
        "detail": {
            "motd": flatten_motd(data.get("description")).strip(),
            "version": (data.get("version") or {}).get("name"),
            "online": players.get("online"),
            "max": players.get("max"),
            "sample": sample[:12],
            "favicon": data.get("favicon") if str(
                data.get("favicon", "")).startswith("data:image/png;base64,") else None,
        },
    }


def probe_tcp(host: str, port: int, timeout: float) -> dict[str, Any]:
    t0 = time.monotonic()
    with socket.create_connection((host, port), timeout):
        pass
    return {"up": True, "latency_ms": round((time.monotonic() - t0) * 1000), "detail": {}}


def probe_http(url: str, timeout: float, expect: Any, headers: dict) -> dict[str, Any]:
    """`expect` is a status code, or "any" for services that answer but need auth
    (a reverse proxy returning 401 is reachable, which is what we are asking)."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": "pi-dash", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, body = r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        status, body = e.code, b""
    ms = round((time.monotonic() - t0) * 1000)
    if expect == "any":
        ok = True
    elif expect:
        ok = status == int(expect)
    else:
        ok = 200 <= status < 400
    detail: dict[str, Any] = {"status": status}
    if body[:1] in (b"{", b"["):
        try:
            detail["json"] = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            pass
    return {"up": ok, "latency_ms": ms, "detail": detail}


def probe_jellyfin(url: str, timeout: float) -> dict[str, Any]:
    """/System/Info/Public needs no API key, so this works out of the box."""
    res = probe_http(url.rstrip("/") + "/System/Info/Public", timeout, 200, {})
    info = res["detail"].pop("json", {}) or {}
    res["detail"] = {
        "status": res["detail"].get("status"),
        "server": info.get("ServerName"),
        "version": info.get("Version"),
        "product": info.get("ProductName"),
    }
    return res


def run_probe(svc: dict[str, Any]) -> dict[str, Any]:
    kind = svc.get("type", "tcp")
    timeout = float(svc.get("timeout", 3))
    host = svc.get("host", "127.0.0.1")
    out: dict[str, Any] = {
        "name": svc.get("name") or f"{host}:{svc.get('port')}",
        "type": kind,
        "link": svc.get("link"),
        "note": svc.get("note"),
        "target": svc.get("url") or f"{host}:{svc.get('port')}",
    }
    try:
        if kind == "minecraft":
            out |= probe_minecraft(host, int(svc.get("port", 25565)), timeout)
        elif kind == "jellyfin":
            out |= probe_jellyfin(svc["url"], timeout)
        elif kind == "http":
            out |= probe_http(svc["url"], timeout, svc.get("expect"), svc.get("headers") or {})
        elif kind == "tcp":
            out |= probe_tcp(host, int(svc["port"]), timeout)
        else:
            out |= {"up": False, "error": f"unknown probe type {kind!r}"}
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        out |= {"up": False, "error": f"{type(e).__name__}: {e}"}
    return out


def load_services() -> list[dict[str, Any]]:
    if not SERVICES_FILE.exists():
        return []
    try:
        raw = json.loads(SERVICES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    services = raw.get("services", raw) if isinstance(raw, dict) else raw
    return services if isinstance(services, list) else []


async def probe_all() -> dict[str, Any]:
    services = load_services()
    if not services:
        return {"services": [], "configured": False, "config_path": str(SERVICES_FILE)}
    results = await asyncio.gather(*(asyncio.to_thread(run_probe, s) for s in services))
    return {"services": list(results), "configured": True, "config_path": str(SERVICES_FILE)}


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_: FastAPI):
    HISTORY.open()
    if not HISTORY.enabled:
        print(f"!! history disabled: {HISTORY.reason}")
    task = asyncio.create_task(history_loop()) if HISTORY.enabled else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(
    title="pi-dash", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
)

# Everything the page needs is served from this origin, so the policy can be
# closed all the way down. frame-ancestors is the load-bearing one: without it
# any site you visit can iframe the dashboard, which auto-authenticates from
# localStorage, and bait clicks onto the unit controls or the terminal.
CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # Vendored assets are big and immutable; only keep live data out of caches.
    if not request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/metrics", dependencies=[Depends(require_auth)])
async def api_metrics() -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(collect_metrics))


@app.get("/api/services", dependencies=[Depends(require_auth)])
async def api_services() -> JSONResponse:
    return JSONResponse({"units": await asyncio.to_thread(list_units)})


@app.post("/api/services/{unit}/{action}", dependencies=[Depends(require_auth)])
async def api_service_action(unit: str, action: str) -> JSONResponse:
    if not ALLOW_CONTROL:
        raise HTTPException(403, "Service control disabled (PIDASH_ALLOW_CONTROL=0)")
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "action must be start, stop or restart")
    if not UNIT_RE.match(unit):
        raise HTTPException(400, "bad unit name")
    cmd = ["systemctl", action, unit]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    code, out, err = await asyncio.to_thread(run, cmd, 30)
    if code != 0:
        return JSONResponse({"ok": False, "error": err.strip() or out.strip()}, status_code=400)
    return JSONResponse({"ok": True})


@app.get("/api/processes", dependencies=[Depends(require_auth)])
async def api_processes(
    sort: str = Query("cpu", pattern="^(cpu|mem)$"),
    limit: int = Query(30, ge=1, le=300),
    q: str = Query("", max_length=100),
) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(top_processes, limit, sort, q))


@app.get("/api/history", dependencies=[Depends(require_auth)])
async def api_history(
    span: int = Query(21600, ge=300, le=2678400),
    points: int = Query(240, ge=10, le=1000),
) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(HISTORY.series, span, points))


@app.get("/api/ports", dependencies=[Depends(require_auth)])
async def api_ports() -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(listening_sockets))


@app.get("/api/probes", dependencies=[Depends(require_auth)])
async def api_probes() -> JSONResponse:
    return JSONResponse(await probe_all())


@app.get("/api/logs", dependencies=[Depends(require_auth)])
async def api_logs(
    priority: int = Query(3, ge=0, le=7),
    lines: int = Query(300, ge=1, le=2000),
    unit: str | None = None,
    since: str | None = Query(None, max_length=64),
) -> JSONResponse:
    entries = await asyncio.to_thread(read_journal, priority, lines, unit, since)
    return JSONResponse({"entries": entries})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- entry


def resolve_bind() -> str:
    if BIND:
        return BIND
    ts = tailscale_ip()
    if ts:
        return ts
    print("!! No tailscale0 address found. Binding to 127.0.0.1 instead.")
    print("!! Set PIDASH_BIND=0.0.0.0 to override, but understand the exposure first.")
    return "127.0.0.1"


if __name__ == "__main__":
    host = resolve_bind()
    print("\n  pi-dash")
    print(f"  url    http://{host}:{PORT}/")
    # Never print the token itself: under systemd stdout goes to the journal,
    # where anyone in systemd-journal can read it back — including through this
    # dashboard's own Faults tab at priority=info.
    print(f"  token  cat {TOKEN_FILE}")
    uvicorn.run(app, host=host, port=PORT, log_level="warning", ws_ping_interval=20)
