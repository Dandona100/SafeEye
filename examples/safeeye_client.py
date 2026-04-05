"""
SafeEyes Python SDK Client

A lightweight client for the SafeEyes content-safety scanner API.

Requirements:
    pip install requests

Usage:
    from safeeye_client import SafeEyeClient

    client = SafeEyeClient("http://localhost:1985", token="your-api-token")
    result = client.scan_file("/path/to/image.jpg")
    print(result["result"]["is_nsfw"])
"""

import os
from typing import Optional

import requests


class SafeEyeError(Exception):
    """Raised when the SafeEyes API returns an error."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class SafeEyeClient:
    """Client for the SafeEyes content-safety scanner API.

    Args:
        url: Base URL of the SafeEyes server (default: http://localhost:1985).
        token: API bearer token. If not provided, reads from SAFEEYE_TOKEN env var.
        timeout: Request timeout in seconds (default: 60).
    """

    def __init__(
        self,
        url: str = "http://localhost:1985",
        token: Optional[str] = None,
        timeout: int = 60,
    ):
        self.base_url = url.rstrip("/")
        self.token = token or os.environ.get("SAFEEYE_TOKEN", "")
        self.timeout = timeout
        self._session = requests.Session()
        if self.token:
            self._session.headers["Authorization"] = f"Bearer {self.token}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _handle(self, resp: requests.Response) -> dict:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise SafeEyeError(resp.status_code, detail)
        return resp.json()

    # ------------------------------------------------------------------
    # Scan endpoints
    # ------------------------------------------------------------------

    def scan_file(self, path: str) -> dict:
        """Scan a local file for NSFW content.

        Args:
            path: Path to an image or video file.

        Returns:
            Scan result dict with keys: scan_id, result, timestamp.
        """
        with open(path, "rb") as f:
            filename = os.path.basename(path)
            resp = self._session.post(
                self._url("/api/v1/scan/file"),
                files={"file": (filename, f)},
                timeout=self.timeout,
            )
        return self._handle(resp)

    def scan_url(self, url: str) -> dict:
        """Scan a remote URL for NSFW content.

        Args:
            url: Direct link to an image or video.

        Returns:
            Scan result dict with keys: scan_id, result, timestamp.
        """
        resp = self._session.post(
            self._url("/api/v1/scan/url"),
            params={"url": url},
            timeout=self.timeout,
        )
        return self._handle(resp)

    def scan_async(self, url: str = None, path: str = None, webhook_url: str = None) -> dict:
        """Submit an asynchronous scan. Returns a job_id immediately.

        Args:
            url: Remote URL to scan (mutually exclusive with path).
            path: Local file path to scan (mutually exclusive with url).
            webhook_url: Optional URL to receive the result via POST callback.

        Returns:
            Dict with keys: job_id, status.
        """
        params = {}
        files = None
        if webhook_url:
            params["webhook_url"] = webhook_url

        if url:
            params["url"] = url
            resp = self._session.post(
                self._url("/api/v1/scan/async"),
                params=params,
                timeout=self.timeout,
            )
        elif path:
            with open(path, "rb") as f:
                filename = os.path.basename(path)
                resp = self._session.post(
                    self._url("/api/v1/scan/async"),
                    params=params,
                    files={"file": (filename, f)},
                    timeout=self.timeout,
                )
        else:
            raise ValueError("Provide either url or path")

        return self._handle(resp)

    def scan_batch(self, urls: list[str], webhook_url: str = None) -> dict:
        """Submit multiple URLs for batch scanning.

        Args:
            urls: List of image/video URLs (max 100).
            webhook_url: Optional webhook for result delivery.

        Returns:
            Dict with keys: batch_id, total, status.
        """
        body = {"urls": urls}
        if webhook_url:
            body["webhook_url"] = webhook_url
        resp = self._session.post(
            self._url("/api/v1/scan/batch"),
            json=body,
            timeout=self.timeout,
        )
        return self._handle(resp)

    # ------------------------------------------------------------------
    # Job / Batch polling
    # ------------------------------------------------------------------

    def get_job(self, job_id: str) -> dict:
        """Poll an async job status.

        Args:
            job_id: The job_id returned by scan_async.

        Returns:
            Job status dict with keys: job_id, status, result (if completed).
        """
        resp = self._session.get(
            self._url(f"/api/v1/job/{job_id}"),
            timeout=self.timeout,
        )
        return self._handle(resp)

    def get_batch(self, batch_id: str) -> dict:
        """Get batch progress and results.

        Args:
            batch_id: The batch_id returned by scan_batch.

        Returns:
            Batch status dict with progress and per-URL results.
        """
        resp = self._session.get(
            self._url(f"/api/v1/batch/{batch_id}"),
            timeout=self.timeout,
        )
        return self._handle(resp)

    # ------------------------------------------------------------------
    # Stats & History
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Get scanner statistics overview.

        Returns:
            Stats dict with keys: total_scans, nsfw_detected, safe_detected, etc.
        """
        resp = self._session.get(
            self._url("/api/v1/stats"),
            timeout=self.timeout,
        )
        return self._handle(resp)

    def get_provider_stats(self) -> list[dict]:
        """Get per-provider statistics.

        Returns:
            List of provider stat dicts.
        """
        resp = self._session.get(
            self._url("/api/v1/stats/providers"),
            timeout=self.timeout,
        )
        return self._handle(resp)

    def get_history(
        self,
        limit: int = 50,
        offset: int = 0,
        nsfw_only: bool = False,
    ) -> list[dict]:
        """Get scan history.

        Args:
            limit: Max results to return (default 50, max 200).
            offset: Pagination offset.
            nsfw_only: If True, return only NSFW-flagged scans.

        Returns:
            List of history item dicts.
        """
        resp = self._session.get(
            self._url("/api/v1/stats/history"),
            params={"limit": limit, "offset": offset, "nsfw_only": nsfw_only},
            timeout=self.timeout,
        )
        return self._handle(resp)

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def submit_feedback(self, scan_id: str, actual_nsfw: bool, notes: str = "") -> dict:
        """Submit feedback on a scan result (for model improvement).

        Args:
            scan_id: The scan ID to provide feedback for.
            actual_nsfw: Whether the content is actually NSFW.
            notes: Optional notes.

        Returns:
            Status dict.
        """
        resp = self._session.post(
            self._url(f"/api/v1/feedback/{scan_id}"),
            json={"actual_nsfw": actual_nsfw, "notes": notes},
            timeout=self.timeout,
        )
        return self._handle(resp)

    # ------------------------------------------------------------------
    # Scan by ID
    # ------------------------------------------------------------------

    def get_scan(self, scan_id: str) -> dict:
        """Retrieve a specific scan result by ID.

        Args:
            scan_id: The scan ID.

        Returns:
            Scan result dict.
        """
        resp = self._session.get(
            self._url(f"/api/v1/scan/{scan_id}"),
            timeout=self.timeout,
        )
        return self._handle(resp)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """Check if the server is reachable.

        Returns:
            Server health/info dict.
        """
        resp = self._session.get(
            self._url("/docs"),
            timeout=10,
        )
        return {"status": "ok", "http_code": resp.status_code}

    def __repr__(self) -> str:
        return f"SafeEyeClient(url={self.base_url!r})"
