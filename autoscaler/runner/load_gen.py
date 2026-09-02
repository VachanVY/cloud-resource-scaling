"""
HTTP load generator.

Sends requests to the target service at a *time-varying* target RPS
defined by a workload generator.  Each second it:
  1. Determines the target RPS for this second.
  2. Spreads that many requests evenly over the second using sub-second
     sleep intervals.
  3. Dispatches each request from a thread-pool worker.
  4. Records the response latency and status code.

Thread safety: all per-request results are appended to a list protected
by a lock.  The experiment runner drains this list every SCALING_INTERVAL
seconds to compute metric snapshots.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests as http_lib


@dataclass
class RequestRecord:
    timestamp: float         # wall time of request dispatch
    latency_ms: float        # end-to-end measured latency (ms)
    status_code: int         # HTTP status (503 = overloaded / timeout)
    queue_wait_ms: float = 0.0
    service_ms: float = 0.0


class LoadGenerator:
    """Continuously generates HTTP load against the target service."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8100",
        max_workers: int = 100,
        request_timeout_s: float = 6.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_workers = max_workers
        self.request_timeout_s = request_timeout_s

        self._records: list[RequestRecord] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="load")

        # Injected by the experiment runner each second
        self._current_rps: float = 0.0

    # ── Control ───────────────────────────────────────────────────────────────

    def start(self, initial_rps: float = 10.0) -> None:
        self._current_rps = initial_rps
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="load-driver")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def set_rps(self, rps: float) -> None:
        self._current_rps = max(0.0, rps)

    # ── Record collection ─────────────────────────────────────────────────────

    def drain_records(self) -> list[RequestRecord]:
        """Return and clear all accumulated records since last drain."""
        with self._lock:
            records = self._records[:]
            self._records.clear()
        return records

    def peek_records(self) -> list[RequestRecord]:
        """Return records without clearing (for concurrent reads)."""
        with self._lock:
            return self._records[:]

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            rps = self._current_rps
            if rps <= 0:
                time.sleep(0.1)
                continue

            interval_s = 1.0 / rps
            deadline = time.monotonic() + interval_s
            self._executor.submit(self._send_request)
            # Sleep until next request is due
            sleep = deadline - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)

    def _send_request(self) -> None:
        t0 = time.monotonic()
        try:
            resp = http_lib.get(
                f"{self.base_url}/process",
                timeout=self.request_timeout_s,
            )
            latency_ms = (time.monotonic() - t0) * 1000.0
            data = resp.json() if resp.content else {}
            record = RequestRecord(
                timestamp=t0,
                latency_ms=latency_ms,
                status_code=resp.status_code,
                queue_wait_ms=data.get("queue_wait_ms", 0.0),
                service_ms=data.get("service_ms", 0.0),
            )
        except Exception:  # noqa: BLE001
            latency_ms = (time.monotonic() - t0) * 1000.0
            record = RequestRecord(
                timestamp=t0,
                latency_ms=latency_ms,
                status_code=503,
            )
        with self._lock:
            self._records.append(record)
