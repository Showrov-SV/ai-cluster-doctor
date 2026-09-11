"""
agent_core.py
Core Cluster Doctor Agent logic - platform-independent. This is the part
that actually gets tested; windows_service.py is a thin Windows Service
wrapper around AgentRunner.run_forever().

Phase 1 scope only:
- Generate/load a persistent device_id that survives restarts/reinstalls
- Auto-discover the Host on the LAN via UDP broadcast (no manual IP config)
- Collect a telemetry reading (best-effort real metrics via psutil)
- POST it to the Host's /api/agent/telemetry endpoint
- Auto-reconnect: on any failure (Host down, network blip, discovery miss),
  back off and retry indefinitely rather than crashing

Explicitly NOT in scope for Phase 1: a redesigned CPU/RAM-native telemetry
schema and matching ML thresholds. This agent maps what it can onto the
*existing* 5-key GPU-oriented contract (temperature_c, gpu_utilization_pct,
memory_utilization_pct, power_watts, fan_speed_pct) so the Host/ml.py need
zero changes, per the "TelemetryProvider compatibility" requirement.
Fields with no reliable cross-platform source report 0.0 - an honest
placeholder, not a fabricated reading.
"""

from __future__ import annotations
import logging
import os
import platform
import socket
import time
import uuid
from pathlib import Path
from typing import Optional

import psutil
import requests

logger = logging.getLogger("cluster_doctor_agent")

DISCOVERY_PORT = 47110
DISCOVERY_REQUEST = b"CLUSTER_DOCTOR_DISCOVER"
DISCOVERY_REPLY_PREFIX = b"CLUSTER_DOCTOR_HOST:"
DISCOVERY_TIMEOUT_S = 3.0
# Override for testing without a real LAN broadcast domain (see README).
DISCOVERY_TARGET = os.environ.get("CLUSTER_DOCTOR_DISCOVERY_ADDR", "255.255.255.255")

SEND_INTERVAL_S = 5.0
RECONNECT_BACKOFF_S = [2, 5, 10, 30, 60]  # caps at 60s between retries
# Must match the Host's agent API key (printed in the Host's startup logs,
# or set explicitly via CLUSTER_DOCTOR_AGENT_API_KEY on the Host).
API_KEY = os.environ.get("CLUSTER_DOCTOR_API_KEY", "")


def _device_id_path() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "ClusterDoctorAgent"
    else:
        base = Path("/etc/cluster-doctor-agent")
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base / "device_id.txt"
    except PermissionError:
        # fall back to a user-writable location (useful for sandboxes/testing
        # where the system-wide path isn't writable without admin rights)
        fallback = Path.home() / ".cluster_doctor_agent"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / "device_id.txt"


def get_or_create_device_id() -> str:
    """Persisted once on disk, so reinstalling or restarting the Agent
    doesn't register as a brand-new device on the Host."""
    try:
        path = _device_id_path()
        if path.exists():
            existing = path.read_text().strip()
            if existing:
                return existing
        new_id = str(uuid.uuid4())
        path.write_text(new_id)
        return new_id
    except OSError as exc:
        # Both the system-wide path and the ~/.cluster_doctor_agent fallback
        # in _device_id_path() failed (e.g. a fully locked-down environment).
        # Don't let this stop the Agent from running - fall back to an
        # in-memory id for this process's lifetime and say exactly why.
        fallback_id = str(uuid.uuid4())
        logger.warning(
            "Could not persist device_id to disk (%s); using a temporary "
            "in-memory id for this run. This device will re-register as new "
            "on every restart until this is fixed.", exc,
        )
        return fallback_id


def discover_host(timeout: float = DISCOVERY_TIMEOUT_S) -> Optional[str]:
    """Broadcasts a discovery request on the LAN and returns the Host's
    base URL (e.g. "http://192.168.1.20:8000"), or None if nothing replied
    within the timeout or discovery couldn't be attempted (e.g. no network
    adapter is up yet, which commonly happens right after boot)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        sock.sendto(DISCOVERY_REQUEST, (DISCOVERY_TARGET, DISCOVERY_PORT))
        data, addr = sock.recvfrom(1024)
        if data.startswith(DISCOVERY_REPLY_PREFIX):
            payload = data[len(DISCOVERY_REPLY_PREFIX):].decode().strip()
            parts = payload.split(":")
            port = parts[0]
            scheme = parts[1] if len(parts) > 1 else "http"
            return f"{scheme}://{addr[0]}:{port}"
    except socket.timeout:
        logger.warning("Host discovery timed out - no Host responded on the LAN")
    except OSError as exc:
        # Covers: no network adapter up yet at boot, broadcast blocked by a
        # firewall/permission, and the "port unreachable" ConnectionResetError
        # Windows raises on a UDP broadcast when nothing's listening. Any of
        # these used to crash the Agent outright; now they're a normal,
        # retryable discovery failure just like a timeout.
        logger.warning("Host discovery failed (%s) - will retry", exc)
    finally:
        sock.close()
    return None


def collect_reading() -> dict:
    """Best-effort mapping of real machine metrics onto the existing 5-key
    telemetry contract."""
    temp_c = 0.0
    try:
        sensor_groups = psutil.sensors_temperatures()
        if sensor_groups:
            first_group = next(iter(sensor_groups.values()))
            if first_group:
                temp_c = first_group[0].current
    except AttributeError:
        pass  # not available on this platform (e.g. most macOS setups)

    mem_pct = psutil.virtual_memory().percent
    cpu_pct = psutil.cpu_percent(interval=0.5)  # stand-in for gpu_utilization_pct until a GPU provider exists

    fan_pct = 0.0
    try:
        fan_groups = psutil.sensors_fans()
        if fan_groups:
            first_group = next(iter(fan_groups.values()))
            if first_group:
                fan_pct = first_group[0].current
    except AttributeError:
        pass

    return {
        "temperature_c": round(temp_c, 1),
        "gpu_utilization_pct": round(cpu_pct, 1),  # placeholder until a GPU provider is added
        "memory_utilization_pct": round(mem_pct, 1),
        "power_watts": 0.0,  # no reliable cross-platform source yet
        "fan_speed_pct": round(fan_pct, 1),
    }


class AgentRunner:
    """Owns the discovery/send/reconnect loop. `run_forever()` blocks, so
    the Windows Service wrapper runs it on a background thread."""

    def __init__(self):
        self.device_id = get_or_create_device_id()
        self.hostname = socket.gethostname()
        # Optional manual override (skips discovery entirely if set)
        self.host_url: Optional[str] = os.environ.get("CLUSTER_DOCTOR_HOST_URL")
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _ensure_host(self) -> bool:
        if self.host_url:
            return True
        logger.info("Discovering Host on the LAN...")
        self.host_url = discover_host()
        return self.host_url is not None

    def _send_once(self) -> bool:
        if not self._ensure_host():
            return False

        try:
            reading = collect_reading()
        except Exception as exc:  # a bad sensor/driver on this machine shouldn't kill the Agent
            logger.warning("Failed to collect a telemetry reading (%s) - will retry", exc)
            return False

        try:
            resp = requests.post(
                f"{self.host_url}/api/agent/telemetry",
                json={"device_id": self.device_id, "hostname": self.hostname, "reading": reading},
                headers={"X-API-Key": API_KEY} if API_KEY else {},
                timeout=5,
                verify=os.environ.get("CLUSTER_DOCTOR_VERIFY_TLS", "true").lower() != "false",
            )
            if resp.status_code in (401, 403):
                # Wrong/missing CLUSTER_DOCTOR_API_KEY. No amount of retrying
                # fixes this - say so plainly instead of logging it as a
                # generic connection failure, which sends people chasing
                # a network problem that doesn't exist.
                logger.error(
                    "Host at %s rejected this Agent's API key (HTTP %s). Set "
                    "CLUSTER_DOCTOR_API_KEY to match the key shown in the "
                    "Host's startup logs. Will keep retrying in case it's "
                    "updated.", self.host_url, resp.status_code,
                )
                return False
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning("Failed to reach Host (%s): %s - will rediscover", self.host_url, exc)
            self.host_url = None  # force rediscovery next attempt (Host may have moved/restarted)
            return False

    def run_forever(self) -> None:
        backoff_idx = 0
        logger.info("Cluster Doctor Agent starting (device_id=%s, hostname=%s)", self.device_id, self.hostname)
        while not self._stop:
            try:
                ok = self._send_once()
            except Exception as exc:
                # Last-resort safety net. _send_once() already handles the
                # known failure modes (discovery, network, auth, sensors);
                # this only catches something genuinely unanticipated, and
                # makes sure it can never permanently kill the Agent thread.
                logger.error("Unexpected error in Agent loop: %s", exc, exc_info=True)
                ok = False

            if ok:
                backoff_idx = 0
                time.sleep(SEND_INTERVAL_S)
            else:
                wait = RECONNECT_BACKOFF_S[min(backoff_idx, len(RECONNECT_BACKOFF_S) - 1)]
                logger.info("Retrying in %ss...", wait)
                time.sleep(wait)
                backoff_idx += 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    AgentRunner().run_forever()
