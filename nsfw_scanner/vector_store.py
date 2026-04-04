"""Lightweight in-memory vector similarity search using pHash as vectors."""
from typing import List, Tuple


class VectorStore:
    """In-memory store for pHash vectors with cosine similarity search."""

    def __init__(self):
        self.hashes: list = []  # list of (scan_id, bits_list)

    def add(self, scan_id: str, phash_hex: str):
        self.hashes.append((scan_id, self._hex_to_bits(phash_hex)))

    def search(self, query_hex: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """Find most similar hashes. Returns [(scan_id, similarity), ...]"""
        query = self._hex_to_bits(query_hex)
        results = []
        for scan_id, stored in self.hashes:
            sim = self._similarity(query, stored)
            results.append((scan_id, sim))
        results.sort(key=lambda x: -x[1])
        return results[:top_k]

    def _hex_to_bits(self, hex_str: str) -> list:
        """Convert a hex hash string to a list of bit integers."""
        return [int(b) for b in bin(int(hex_str, 16))[2:].zfill(64)]

    def _similarity(self, a: list, b: list) -> float:
        """Compute similarity as 1 - normalised Hamming distance."""
        mismatches = sum(x != y for x, y in zip(a, b))
        return 1.0 - mismatches / max(len(a), 1)

    def load_from_db(self, scans: list):
        """Load existing scans from DB rows."""
        for s in scans:
            phash = s.get("phash") if isinstance(s, dict) else s["phash"]
            scan_id = s.get("id") if isinstance(s, dict) else s["id"]
            if phash:
                self.add(scan_id, phash)

    def __len__(self) -> int:
        return len(self.hashes)
