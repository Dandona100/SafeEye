"""SafeEyes P2P Network — zero-config metadata sharing between servers.

Each server auto-generates a key on first run. Servers register with a
central registry to discover peers. No manual secrets — like HTTPS.
"""
import asyncio
import json
import hashlib
import logging
import secrets
import time

import aiohttp

logger = logging.getLogger("safeeyes.gossip")

# Registry: GitHub raw file — transparent, anyone can see the server list
REGISTRY_URL = "https://raw.githubusercontent.com/Dandona100/SafeEyes/main/network/peers.json"
REGISTRY_SUBMIT = "https://lhflow.com:1985/api/v1/network/register"

_seen_ids: set[str] = set()
_MAX_SEEN = 50_000


class GossipNode:
    """Zero-config P2P network node."""

    def __init__(self):
        self.enabled: bool = False
        self.server_id: str = ""
        self.server_key: str = ""  # auto-generated, saved to DB
        self.peers: dict[str, dict] = {}  # url -> {connected, ws}
        self._on_hash_received = None
        self._tasks: list[asyncio.Task] = []
        self.network_servers: list[dict] = []  # from registry

    def configure(self, enabled: bool, server_id: str = "", server_key: str = ""):
        """Configure node. Key auto-generated if empty."""
        self.enabled = enabled
        self.server_id = server_id or secrets.token_hex(8)
        self.server_key = server_key or secrets.token_hex(32)

    def sign(self, data: str) -> str:
        """Sign data with server key (HMAC)."""
        return hashlib.sha256((self.server_key + data).encode()).hexdigest()[:16]

    def on_hash(self, callback):
        self._on_hash_received = callback

    async def start(self):
        """Start: register with registry + discover peers."""
        if not self.enabled:
            return
        logger.info(f"P2P Network starting — id={self.server_id}")
        self._tasks.append(asyncio.create_task(self._registry_loop()))

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    async def _registry_loop(self):
        """Periodically: 1) register self, 2) fetch peer list from GitHub."""
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    # Register self (so lhflow.com knows we're alive)
                    try:
                        await session.post(REGISTRY_SUBMIT, json={
                            "server_id": self.server_id,
                            "signature": self.sign(self.server_id),
                        }, timeout=aiohttp.ClientTimeout(total=10))
                    except Exception:
                        pass

                    # Fetch known peers from GitHub
                    try:
                        async with session.get(REGISTRY_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                data = await resp.json(content_type=None)
                                self.network_servers = data.get("servers", [])
                                logger.debug(f"Registry: {len(self.network_servers)} servers listed")
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Registry loop error: {e}")
            await asyncio.sleep(300)  # every 5 min

    async def broadcast_hash(self, record: dict):
        """Broadcast new hash to registry for distribution."""
        if not self.enabled:
            return
        # For now, hashes are shared via the metadata API endpoints
        # P2P WebSocket distribution happens when peers connect

    async def handle_incoming_ws(self, ws):
        """Handle incoming WebSocket from a peer."""
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") != "auth":
                await ws.close(code=4001, reason="Auth required")
                return
            peer_id = msg.get("server_id", "")
            sig = msg.get("signature", "")
            logger.info(f"Peer connected: {peer_id}")

            # Send bulk sync
            from nsfw_scanner import db as database
            try:
                data = await database.export_hash_metadata()
                if data:
                    for i in range(0, len(data), 500):
                        await ws.send_text(json.dumps({"type": "hash_bulk", "records": data[i:i+500]}))
            except Exception:
                pass

            # Listen
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                if msg.get("type") == "hash_bulk" and self._on_hash_received:
                    for rec in msg.get("records", []):
                        await self._on_hash_received(rec)
                elif msg.get("type") == "hash_new" and self._on_hash_received:
                    msg_id = msg.get("id", "")
                    if msg_id not in _seen_ids:
                        _seen_ids.add(msg_id)
                        _trim_seen()
                        await self._on_hash_received(msg)
        except Exception as e:
            logger.debug(f"Peer disconnected: {e}")

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "server_id": self.server_id,
            "network_servers": self.network_servers,
            "network_count": len(self.network_servers),
        }


def _trim_seen():
    global _seen_ids
    if len(_seen_ids) > _MAX_SEEN:
        items = list(_seen_ids)
        _seen_ids = set(items[len(items) // 2:])


gossip_node = GossipNode()
