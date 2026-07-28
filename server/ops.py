"""Whitelisted operations against the droplet the bot runs on.

Every command here is a fixed argv list. Nothing the model or the browser sends
is ever interpolated into a shell string -- arguments are validated against a
discovered allowlist first, and subprocesses run with ``shell=False``.

The one exception is :func:`run_shell`, which exists because sooner or later you
will need something this file does not cover. It is disabled unless
``AGENT_ALLOW_SHELL=true``, and even then it is only ever reached through the
confirmation queue, so the exact string is on screen before it runs.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from typing import Any

from . import config

#: systemd unit names are conservative on purpose.
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]{1,72}$")
_DEFAULT_UNIT_PREFIXES = ("sniper", "pumpbot", "definitivo", "bot-sniper")

_TIMEOUT = 25


class OpsError(RuntimeError):
    """A refused or failed droplet operation."""


async def _run(argv: list[str], timeout: int = _TIMEOUT) -> dict[str, Any]:
    """Run a command with no shell, capturing both streams."""
    if shutil.which(argv[0]) is None:
        raise OpsError(f"'{argv[0]}' no esta disponible en este servidor")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError as exc:
        raise OpsError(f"'{argv[0]}' no termino en {timeout}s") from exc
    except OSError as exc:
        raise OpsError(f"No pude ejecutar {argv[0]}: {exc}") from exc

    return {
        "command": " ".join(argv),
        "exit_code": process.returncode,
        "stdout": stdout.decode(errors="replace").strip(),
        "stderr": stderr.decode(errors="replace").strip(),
    }


async def _run_shell(command: str, timeout: int = _TIMEOUT) -> dict[str, Any]:
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(config.BOT_REPO) if config.BOT_REPO.is_dir() else None,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError as exc:
        process.kill()
        raise OpsError(f"El comando no termino en {timeout}s") from exc
    return {
        "command": command,
        "exit_code": process.returncode,
        "stdout": stdout.decode(errors="replace").strip()[:8000],
        "stderr": stderr.decode(errors="replace").strip()[:4000],
    }


# --------------------------------------------------------------------------- #
# Unit discovery                                                              #
# --------------------------------------------------------------------------- #


async def known_units() -> list[str]:
    """systemd units this panel may touch.

    An explicit ``PANEL_ALLOWED_UNITS`` wins. Otherwise units are discovered
    from systemd and filtered by name prefix, so a panel installed next to an
    unrelated service cannot restart it by accident.
    """
    if config.ALLOWED_UNITS:
        return list(config.ALLOWED_UNITS)
    if shutil.which("systemctl") is None:
        return []
    result = await _run(
        ["systemctl", "list-units", "--type=service", "--all", "--no-legend",
         "--plain", "--no-pager"]
    )
    units: list[str] = []
    for line in result["stdout"].splitlines():
        name = line.split()[0] if line.split() else ""
        if name.endswith(".service") and name.lower().startswith(_DEFAULT_UNIT_PREFIXES):
            units.append(name)
    return units


async def _check_unit(unit: str) -> str:
    unit = (unit or "").strip()
    if not _UNIT_RE.match(unit):
        raise OpsError(f"Nombre de unidad invalido: {unit!r}")
    allowed = await known_units()
    if unit not in allowed:
        raise OpsError(
            f"'{unit}' no esta en la lista permitida. Permitidas: "
            f"{', '.join(allowed) or '(ninguna detectada)'}. "
            "Anade PANEL_ALLOWED_UNITS al .env si falta alguna."
        )
    return unit


# --------------------------------------------------------------------------- #
# Read-only                                                                   #
# --------------------------------------------------------------------------- #


async def health() -> dict[str, Any]:
    """Load, memory, disk and whether the bot processes are alive."""
    out: dict[str, Any] = {"platform": os.name}

    try:
        load1, load5, load15 = os.getloadavg()
        out["load"] = {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)}
    except (OSError, AttributeError):
        out["load"] = None

    usage = shutil.disk_usage(str(config.BOT_REPO if config.BOT_REPO.is_dir() else "/"))
    out["disk"] = {
        "total_gb": round(usage.total / 1e9, 1),
        "used_gb": round(usage.used / 1e9, 1),
        "free_gb": round(usage.free / 1e9, 1),
        "used_pct": round(usage.used / usage.total * 100, 1),
    }

    if shutil.which("free"):
        memory = await _run(["free", "-m"])
        out["memory_raw"] = memory["stdout"]

    out["bot_processes"] = await bot_processes()
    out["units"] = []
    for unit in await known_units():
        status = await _run(["systemctl", "is-active", unit])
        out["units"].append({"unit": unit, "state": status["stdout"] or "unknown"})
    return out


async def bot_processes() -> list[dict[str, Any]]:
    """Running bot_runner / dashboard / panel processes, with CPU and memory."""
    if shutil.which("ps") is None:
        return []
    result = await _run(["ps", "-eo", "pid,pcpu,pmem,etime,args", "--no-headers"])
    rows: list[dict[str, Any]] = []
    for line in result["stdout"].splitlines():
        if not any(k in line for k in ("bot_runner", "pump_bot", "tg_panel", "watchdog")):
            continue
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, cpu, mem, elapsed, args = parts
        rows.append(
            {
                "pid": int(pid),
                "cpu_pct": float(cpu),
                "mem_pct": float(mem),
                "uptime": elapsed,
                "command": args[:180],
            }
        )
    return rows


async def journal(unit: str, lines: int = 60) -> dict[str, Any]:
    """Recent journald output for an allowed unit."""
    unit = await _check_unit(unit)
    lines = max(1, min(int(lines), 300))
    return await _run(
        ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output", "short"]
    )


# --------------------------------------------------------------------------- #
# State-changing (always routed through the confirmation queue)               #
# --------------------------------------------------------------------------- #


async def restart_unit(unit: str) -> dict[str, Any]:
    """Restart an allowed systemd unit."""
    unit = await _check_unit(unit)
    result = await _run(["systemctl", "restart", unit], timeout=60)
    status = await _run(["systemctl", "is-active", unit])
    return {**result, "unit": unit, "state_after": status["stdout"]}


async def stop_unit(unit: str) -> dict[str, Any]:
    """Stop an allowed systemd unit."""
    unit = await _check_unit(unit)
    result = await _run(["systemctl", "stop", unit], timeout=60)
    status = await _run(["systemctl", "is-active", unit])
    return {**result, "unit": unit, "state_after": status["stdout"]}


async def run_shell(command: str) -> dict[str, Any]:
    """Run an arbitrary command in the bot repo. Off unless explicitly enabled.

    This is the escape hatch, not the main road. It is gated twice: by
    ``AGENT_ALLOW_SHELL`` and by the confirmation queue, which shows you the
    exact string before anything runs.
    """
    if not config.AGENT_ALLOW_SHELL:
        raise OpsError(
            "La shell libre esta desactivada. Pon AGENT_ALLOW_SHELL=true en el "
            ".env del panel si de verdad la quieres."
        )
    command = (command or "").strip()
    if not command:
        raise OpsError("Comando vacio")
    return await _run_shell(command)
