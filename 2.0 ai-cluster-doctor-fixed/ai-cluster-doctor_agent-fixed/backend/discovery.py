"""
discovery.py
LAN auto-discovery responder for the Host, so Agents can find it without
any manual IP configuration.

Protocol: an Agent broadcasts b"CLUSTER_DOCTOR_DISCOVER" on UDP port
DISCOVERY_PORT. This listener replies directly to the sender with
b"CLUSTER_DOCTOR_HOST:<api_port>" (the sender's IP is already known from
the UDP packet itself, so the Agent can build the full Host URL).
"""

from __future__ import annotations
import asyncio
import os

DISCOVERY_PORT = 47110
DISCOVERY_REQUEST = b"CLUSTER_DOCTOR_DISCOVER"
DISCOVERY_REPLY_PREFIX = b"CLUSTER_DOCTOR_HOST:"

# Must match whatever port uvicorn is actually serving on (default 8000 per
# README's `uvicorn main:app --port 8000`). Override via env var if running
# on a different port.
API_PORT = int(os.environ.get("CLUSTER_DOCTOR_PORT", "8000"))
# "http" or "https" - set to "https" if the Host is run with --ssl-keyfile/--ssl-certfile
API_SCHEME = os.environ.get("CLUSTER_DOCTOR_SCHEME", "http")


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, api_port: int, api_scheme: str):
        self.api_port = api_port
        self.api_scheme = api_scheme
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if data == DISCOVERY_REQUEST:
            reply = DISCOVERY_REPLY_PREFIX + f"{self.api_port}:{self.api_scheme}".encode()
            self.transport.sendto(reply, addr)


async def start_discovery_responder(api_port: int = API_PORT, api_scheme: str = API_SCHEME):
    """Starts the UDP discovery responder in the background. Call once
    during Host startup (lifespan) and close the returned transport on
    shutdown."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DiscoveryProtocol(api_port, api_scheme),
        local_addr=("0.0.0.0", DISCOVERY_PORT),
        allow_broadcast=True,
    )
    return transport
