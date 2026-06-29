"""Pre-flight check before starting the lab stack.

Verifies: Docker available, Compose v2, enough free RAM, ports unbound.
Writes setup-report.json — commit it to submission/ for the rubric checkpoint.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REQUIRED_PORTS = [8000, 9090, 9093, 3000, 3100, 16686, 4317, 4318, 8888]
MIN_RAM_GB = 4.0
REPORT_PATH = Path(__file__).parent / "setup-report.json"


def check_docker() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "docker binary not found in PATH"
    try:
        out = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"docker version timed out: {e}"
    if out.returncode != 0:
        return False, f"docker daemon not reachable: {out.stderr.strip()}"
    return True, out.stdout.strip()


def check_compose_v2() -> tuple[bool, str]:
    out = subprocess.run(
        ["docker", "compose", "version", "--short"],
        capture_output=True, text=True, timeout=10,
    )
    if out.returncode != 0:
        return False, "Compose v2 not installed (need `docker compose`, not `docker-compose`)"
    return True, out.stdout.strip()


def check_ram_headroom() -> tuple[bool, float]:
    """Best-effort RAM check via Docker info."""
    out = subprocess.run(
        ["docker", "info", "--format", "{{.MemTotal}}"],
        capture_output=True, text=True, timeout=10,
    )
    if out.returncode != 0:
        return False, 0.0
    try:
        gb = int(out.stdout.strip()) / (1024**3)
    except ValueError:
        return False, 0.0
    return gb >= MIN_RAM_GB, round(gb, 2)


def check_port(port: int) -> bool:
    """True if port is free (not bound)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main() -> int:
    docker_ok, docker_ver = check_docker()
    # Compose/RAM checks shell out to `docker`; skip them if the binary is
    # absent so the report still gets written instead of crashing.
    if docker_ok:
        compose_ok, compose_ver = check_compose_v2()
        ram_ok, ram_gb = check_ram_headroom()
    else:
        compose_ok, compose_ver = False, "skipped: docker unavailable"
        ram_ok, ram_gb = False, 0.0
    port_status = {p: check_port(p) for p in REQUIRED_PORTS}
    bound_ports = [p for p, free in port_status.items() if not free]

    report = {
        "docker": {"ok": docker_ok, "version": docker_ver},
        "compose_v2": {"ok": compose_ok, "version": compose_ver},
        "ram_gb_available": ram_gb,
        "ram_ok": ram_ok,
        "required_ports": REQUIRED_PORTS,
        "bound_ports": bound_ports,
        "all_ports_free": not bound_ports,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2))

    print(f"Docker:        {'OK' if docker_ok else 'FAIL'}  ({docker_ver})")
    print(f"Compose v2:    {'OK' if compose_ok else 'FAIL'}  ({compose_ver})")
    print(f"RAM available: {ram_gb} GB ({'OK' if ram_ok else f'NEED >= {MIN_RAM_GB} GB'})")
    print(f"Ports free:    {'OK' if not bound_ports else f'BOUND: {bound_ports}'}")
    print(f"Report written: {REPORT_PATH}")

    return 0 if (docker_ok and compose_ok and ram_ok and not bound_ports) else 1


if __name__ == "__main__":
    sys.exit(main())
