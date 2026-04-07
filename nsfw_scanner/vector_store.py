"""Vector similarity search for pHash fingerprints.

Uses FAISS (Facebook AI Similarity Search) for O(log N) lookups.
Falls back to brute-force if FAISS is not available.
"""
import logging
from typing import List, Tuple

logger = logging.getLogger("safeeyes.vector_store")

try:
    import numpy as np
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False

HASH_BITS = 64  # 64-bit pHash


class VectorStore:
    """pHash similarity search — FAISS-backed with brute-force fallback."""

    def __init__(self):
        self._ids: list[str] = []       # scan_id at each index
        self._id_set: set[str] = set()  # for dedup
        if HAS_FAISS:
            # Binary index — Hamming distance on 64-bit hashes
            self._index = faiss.IndexBinaryFlat(HASH_BITS)
            logger.info("VectorStore: FAISS binary index initialized")
        else:
            self._index = None
            self._hashes: list[list[int]] = []
            logger.info("VectorStore: brute-force fallback (install faiss-cpu for speed)")

    def add(self, scan_id: str, phash_hex: str):
        """Add a pHash to the index."""
        if scan_id in self._id_set:
            return
        self._id_set.add(scan_id)
        self._ids.append(scan_id)

        if HAS_FAISS:
            vec = self._hex_to_bytes(phash_hex)
            self._index.add(vec)
        else:
            self._hashes.append(self._hex_to_bits(phash_hex))

    def search(self, query_hex: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """Find most similar hashes. Returns [(scan_id, similarity), ...]."""
        if len(self._ids) == 0:
            return []

        if HAS_FAISS:
            vec = self._hex_to_bytes(query_hex)
            k = min(top_k, len(self._ids))
            distances, indices = self._index.search(vec, k)
            results = []
            for dist, idx in zip(distances[0], indices[0]):
                if idx < 0 or idx >= len(self._ids):
                    continue
                similarity = 1.0 - dist / HASH_BITS
                results.append((self._ids[idx], round(similarity, 4)))
            return results
        else:
            # Brute-force fallback
            query = self._hex_to_bits(query_hex)
            results = []
            for i, stored in enumerate(self._hashes):
                mismatches = sum(x != y for x, y in zip(query, stored))
                sim = 1.0 - mismatches / HASH_BITS
                results.append((self._ids[i], round(sim, 4)))
            results.sort(key=lambda x: -x[1])
            return results[:top_k]

    def batch_add(self, items: list[dict]):
        """Bulk add from DB rows [{id, phash}, ...]."""
        if HAS_FAISS and items:
            new_items = [s for s in items if s.get("phash") and s.get("id") not in self._id_set]
            if not new_items:
                return
            vecs = np.zeros((len(new_items), HASH_BITS // 8), dtype=np.uint8)
            for i, s in enumerate(new_items):
                self._ids.append(s["id"])
                self._id_set.add(s["id"])
                vecs[i] = self._hex_to_bytes_raw(s["phash"])
            self._index.add(vecs)
        else:
            for s in items:
                if s.get("phash"):
                    self.add(s.get("id", ""), s["phash"])

    def load_from_db(self, scans: list):
        """Load existing scans from DB rows."""
        self.batch_add([
            {"id": s.get("id") if isinstance(s, dict) else s["id"],
             "phash": s.get("phash") if isinstance(s, dict) else s["phash"]}
            for s in scans
        ])

    def stats(self) -> dict:
        """Return index statistics."""
        return {
            "backend": "faiss" if HAS_FAISS else "brute_force",
            "total_vectors": len(self._ids),
            "hash_bits": HASH_BITS,
        }

    def __len__(self) -> int:
        return len(self._ids)

    @staticmethod
    def _hex_to_bits(hex_str: str) -> list:
        return [int(b) for b in bin(int(hex_str, 16))[2:].zfill(HASH_BITS)]

    @staticmethod
    def _hex_to_bytes(hex_str: str):
        """Convert hex hash to numpy uint8 array for FAISS binary index."""
        val = int(hex_str, 16)
        raw = val.to_bytes(HASH_BITS // 8, byteorder='big')
        return np.frombuffer(raw, dtype=np.uint8).reshape(1, -1).copy()

    @staticmethod
    def _hex_to_bytes_raw(hex_str: str):
        val = int(hex_str, 16)
        raw = val.to_bytes(HASH_BITS // 8, byteorder='big')
        return np.frombuffer(raw, dtype=np.uint8).copy()
