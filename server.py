#!/usr/bin/env python3
"""
pi-dash: a single-host dashboard for a Raspberry Pi on a tailnet.

Serves metrics, systemd unit state, journal errors, and a pty-backed shell.
Intended to be bound to the Tailscale interface only. See README.md.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import secrets
import shutil
import signal
import struct
import subprocess
import termios
import time
from hmac import compare_digest
from pathlib import Path
from typing import Any

import psutil
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

CONFIG_DIR = Path(os.environ.get("PIDASH_CONFIG", Path.home() / ".config" / "pidash"))
TOKEN_FILE = CONFIG_DIR / "token"

PORT = int(os.environ.get("PIDASH_PORT", "8088"))
BIND = os.environ.get("PIDASH_BIND", "")  # blank means auto-detect tailscale0
SHELL = os.environ.get("PIDASH_SHELL", os.environ.get("SHELL", "/bin/bash"))
ALLOW_CONTROL = os.environ.get("PIDASH_ALLOW_CONTROL", "1") == "1"

# ---------------------------------------------------------------- auth


def load_token() -> str:
    env = os.environ.get("PIDASH_TOKEN")
    if env:
        return env.strip()
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(tok + "\n")
    TOKEN_FILE.chmod(0o600)
    return tok


TOKEN = load_token()


def check_token(supplied: str | None) -> bool:
    return bool(supplied) and compare_digest(supplied, TOKEN)


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


UNIT_RE = re.compile(r"^[A-Za-z0-9@._\-\\:]+\.service$")


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


# ---------------------------------------------------------------- app

app = FastAPI(title="pi-dash", docs_url=None, redoc_url=None, openapi_url=None)


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


@app.get("/api/logs", dependencies=[Depends(require_auth)])
async def api_logs(
    priority: int = Query(3, ge=0, le=7),
    lines: int = Query(300, ge=1, le=2000),
    unit: str | None = None,
    since: str | None = Query(None, max_length=64),
) -> JSONResponse:
    entries = await asyncio.to_thread(read_journal, priority, lines, unit, since)
    return JSONResponse({"entries": entries})


# ---------------------------------------------------------------- terminal


def spawn_shell(cols: int, rows: int) -> tuple[int, subprocess.Popen]:
    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def child_setup() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    env = dict(os.environ)
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "PIDASH": "1"})
    env.pop("PIDASH_TOKEN", None)

    proc = subprocess.Popen(
        [SHELL, "-l"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=child_setup,
        env=env,
        cwd=os.path.expanduser("~"),
        close_fds=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    return master, proc


@app.websocket("/ws/term")
async def ws_term(ws: WebSocket) -> None:
    if not check_token(ws.query_params.get("token")):
        await ws.close(code=4401)
        return
    await ws.accept()

    cols = int(ws.query_params.get("cols", 100) or 100)
    rows = int(ws.query_params.get("rows", 30) or 30)
    master, proc = await asyncio.to_thread(spawn_shell, cols, rows)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(master, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        queue.put_nowait(data or None)

    loop.add_reader(master, on_readable)

    async def pump_out() -> None:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            await ws.send_bytes(chunk)

    out_task = asyncio.create_task(pump_out())

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                os.write(master, data)
            elif (text := msg.get("text")) is not None:
                try:
                    ctrl = json.loads(text)
                except json.JSONDecodeError:
                    os.write(master, text.encode())
                    continue
                if ctrl.get("type") == "resize":
                    size = struct.pack(
                        "HHHH", int(ctrl.get("rows", 30)), int(ctrl.get("cols", 100)), 0, 0
                    )
                    fcntl.ioctl(master, termios.TIOCSWINSZ, size)
                elif ctrl.get("type") == "input":
                    os.write(master, str(ctrl.get("data", "")).encode())
    except Exception:
        pass
    finally:
        loop.remove_reader(master)
        queue.put_nowait(None)
        out_task.cancel()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            os.close(master)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


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
    print(f"  token  {TOKEN}")
    print(f"  stored {TOKEN_FILE}\n")
    uvicorn.run(app, host=host, port=PORT, log_level="warning", ws_ping_interval=20)
