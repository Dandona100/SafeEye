"""Gossip P2P protocol for sharing hash metadata between SafeEyes servers.

Each server connects to 3-5 peers via WebSocket. New hashes propagate with TTL
to prevent loops. Server identity is anonymous (random ID, rotated on restart).

Protocol messages (JSON over WebSocket):
  - auth:      {type:"auth", server_id:"...", secret:"..."}
  - hash_new:  {type:"hash_new", id:"...", p:"...", n:0/1, c:0.95, l:[], ttl:3}
  - hash_bulk: {type:"hash_bulk", records:[...]}
  - stats:     {type:"stats", scans:N, top_provider:"..."}
  - ping/pong: {type:"ping"} / {type:"pong"}
"""
import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger("safeeyes.gossip")

# Anonymous server ID — rotated every restart
SERVER_ID = secrets.token_hex(8)

# Seen message IDs to prevent re-propagation
_seen_ids: set[str] = set()
_MAX_SEEN = 50_000


@dataclass
class Peer:
    url: str  # ws://host:port/api/v1/gossip/ws
    secret: str = ""
    connected: bool = False
    ws: object = None
    retry_at: float = 0


class GossipNode:
    """Manages P2P connections and message propagation."""

    def __init__(self):
        self.peers: dict[str, Peer] = {}  # url -> Peer
        self.enabled: bool = False
        self.shared_secret: str = ""
        self._on_hash_received = None  # callback(record: dict)
        self._on_stats_received = None  # callback(stats: dict)
        self._tasks: list[asyncio.Task] = []
        # Network stats (anonymous aggregates)
        self.network_stats = {
            "active_peers": 0,
            "total_scans_network": 0,
            "top_provider": "",
        }

    def configure(self, enabled: bool, secret: str, peers: list[str]):
        """Configure the gossip node from saved settings."""
        self.enabled = enabled
        self.shared_secret = secret
        for url in peers:
            if url not in self.peers:
                self.peers[url] = Peer(url=url, secret=secret)

    def on_hash(self, callback):
        """Register callback for received hashes."""
        self._on_hash_received = callback

    def on_stats(self, callback):
        """Register callback for received network stats."""
        self._on_stats_received = callback

    async def start(self):
        """Start connecting to all configured peers."""
        if not self.enabled:
            return
        logger.info(f"Gossip starting — server_id={SERVER_ID}, peers={len(self.peers)}")
        for url, peer in self.peers.items():
            task = asyncio.create_task(self._connect_loop(peer))
            self._tasks.append(task)

    async def stop(self):
        """Disconnect all peers."""
        for task in self._tasks:
            task.cancel()
        for peer in self.peers.values():
            if peer.ws:
                try:
                    await peer.ws.close()
                except Exception:
                    pass
            peer.connected = False
        self._tasks.clear()
        logger.info("Gossip stopped")

    async def broadcast_hash(self, record: dict):
        """Broadcast a new hash to all connected peers."""
        if not self.enabled:
            return
        msg_id = secrets.token_hex(6)
        _seen_ids.add(msg_id)
        _trim_seen()
        msg = {
            "type": "hash_new",
            "id": msg_id,
            "p": record.get("p", ""),
            "n": record.get("n", 0),
            "c": record.get("c", 0),
            "l": record.get("l", []),
            "ttl": 3,
        }
        await self._send_all(msg)

    async def broadcast_stats(self, total_scans: int, top_provider: str):
        """Share anonymous stats with peers."""
        if not self.enabled:
            return
        msg = {
            "type": "stats",
            "scans": total_scans,
            "top_provider": top_provider,
        }
        await self._send_all(msg)

    async def handle_incoming_ws(self, ws):
        """Handle an incoming WebSocket connection from a peer."""
        peer_id = "unknown"
        try:
            # Wait for auth message
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") != "auth" or msg.get("secret") != self.shared_secret:
                await ws.close(code=4001, reason="Auth failed")
                return
            peer_id = msg.get("server_id", "unknown")
            logger.info(f"Gossip peer connected (incoming): {peer_id}")

            # Send bulk sync
            await self._send_bulk_sync(ws)

            # Listen for messages
            while True:
                raw = await ws.receive_text()
                await self._handle_message(json.loads(raw), source_ws=ws)
        except Exception as e:
            logger.debug(f"Gossip incoming peer {peer_id} disconnected: {e}")

    async def _connect_loop(self, peer: Peer):
        """Maintain persistent connection to a peer with reconnect."""
        while True:
            if not self.enabled:
                await asyncio.sleep(30)
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    # Convert ws:// URL
                    ws_url = peer.url
                    async with session.ws_connect(ws_url, timeout=15) as ws:
                        peer.ws = ws
                        peer.connected = True

                        # Send auth
                        await ws.send_json({
                            "type": "auth",
                            "server_id": SERVER_ID,
                            "secret": self.shared_secret,
                        })

                        logger.info(f"Gossip connected to peer: {peer.url}")

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(json.loads(msg.data), source_ws=None)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except Exception as e:
                logger.debug(f"Gossip peer {peer.url} connection failed: {e}")
            finally:
                peer.connected = False
                peer.ws = None

            # Reconnect after delay
            await asyncio.sleep(30)

    async def _handle_message(self, msg: dict, source_ws=None):
        """Process a received gossip message."""
        msg_type = msg.get("type", "")

        if msg_type == "ping":
            if source_ws:
                try:
                    await source_ws.send_json({"type": "pong"})
                except Exception:
                    pass
            return

        if msg_type == "pong":
            return

        if msg_type == "hash_new":
            msg_id = msg.get("id", "")
            if msg_id in _seen_ids:
                return  # Already seen
            _seen_ids.add(msg_id)
            _trim_seen()

            # Process the hash
            if self._on_hash_received:
                await self._on_hash_received({
                    "p": msg.get("p", ""),
                    "n": msg.get("n", 0),
                    "c": msg.get("c", 0),
                    "l": msg.get("l", []),
                })

            # Propagate with decremented TTL
            ttl = msg.get("ttl", 0) - 1
            if ttl > 0:
                msg["ttl"] = ttl
                await self._send_all(msg, exclude_ws=source_ws)

        elif msg_type == "hash_bulk":
            records = msg.get("records", [])
            for rec in records:
                if self._on_hash_received:
                    await self._on_hash_received(rec)

        elif msg_type == "stats":
            if self._on_stats_received:
                await self._on_stats_received(msg)
            # Update local network stats
            self.network_stats["total_scans_network"] += msg.get("scans", 0)
            if msg.get("top_provider"):
                self.network_stats["top_provider"] = msg["top_provider"]

    async def _send_all(self, msg: dict, exclude_ws=None):
        """Send message to all connected peers."""
        for peer in self.peers.values():
            if not peer.connected or not peer.ws:
                continue
            if exclude_ws and peer.ws is exclude_ws:
                continue
            try:
                if hasattr(peer.ws, 'send_json'):
                    await peer.ws.send_json(msg)
                else:
                    await peer.ws.send_text(json.dumps(msg))
            except Exception:
                peer.connected = False

    async def _send_bulk_sync(self, ws):
        """Send bulk hash sync to a newly connected peer."""
        # Import here to avoid circular
        from nsfw_scanner import db as database
        try:
            data = await database.export_hash_metadata()
            if data:
                # Send in chunks of 500
                for i in range(0, len(data), 500):
                    chunk = data[i:i + 500]
                    await ws.send_text(json.dumps({
                        "type": "hash_bulk",
                        "records": chunk,
                    }))
        except Exception as e:
            logger.warning(f"Bulk sync failed: {e}")

    def get_status(self) -> dict:
        """Return gossip node status."""
        connected = sum(1 for p in self.peers.values() if p.connected)
        return {
            "enabled": self.enabled,
            "server_id": SERVER_ID,
            "peers_total": len(self.peers),
            "peers_connected": connected,
            "peers": [
                {"url": p.url, "connected": p.connected}
                for p in self.peers.values()
            ],
            "network_stats": self.network_stats,
        }


def _trim_seen():
    """Keep seen set bounded."""
    global _seen_ids
    if len(_seen_ids) > _MAX_SEEN:
        # Keep last half
        items = list(_seen_ids)
        _seen_ids = set(items[len(items) // 2:])


# Singleton
gossip_node = GossipNode()
