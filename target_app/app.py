"""
Target HTTP service with dynamically adjustable concurrency.

Capacity model:
  capacity  = replicas × THREADS_PER_REPLICA
  Requests above capacity queue until a worker is free or the request times out.
  
  utilization = (active_requests / capacity) × 100   ← reported as "CPU %" signal

Latency model:
  service_time  = BASE_LATENCY_MS ± BASE_JITTER_MS   (independent of load)
  latency       = queue_wait_time + service_time
  Queue wait rises sharply as utilization → 100 %.

All parameters are configurable via query-strings or control endpoints.
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
from collections import deque
from typing import Optional

from flask import Flask, jsonify, request

# ── Constants ──────────────────────────────────────────────────────────────────
THREADS_PER_REPLICA: int = int(os.environ.get("THREADS_PER_REPLICA", "2"))
BASE_LATENCY_MS: float = float(os.environ.get("BASE_LATENCY_MS", "80"))
BASE_JITTER_MS: float = float(os.environ.get("BASE_JITTER_MS", "10"))

# ── Service state ──────────────────────────────────────────────────────────────

class ServiceState:
    """Thread-safe service state with adjustable replica count."""

    def __init__(self, initial_replicas: int = 2):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self.replicas: int = initial_replicas
        self._active: int = 0
        self._total: int = 0
        self._failed: int = 0
        # Sliding window of recent latencies (ms) for /metrics
        self._latency_window: deque = deque(maxlen=200)
        self._rng = random.Random()

    @property
    def capacity(self) -> int:
        return self.replicas * THREADS_PER_REPLICA

    def acquire(self, timeout: float = 5.0) -> bool:
        """Block until a worker slot is free.  Returns False on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            self._total += 1
            while self._active >= self.capacity:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._failed += 1
                    return False
                self._cond.wait(timeout=min(remaining, 0.05))
            self._active += 1
            return True

    def release(self, latency_ms: float) -> None:
        with self._cond:
            self._active -= 1
            self._latency_window.append(latency_ms)
            self._cond.notify()

    def set_replicas(self, n: int) -> None:
        with self._cond:
            self.replicas = max(1, min(20, n))
            # Wake all waiters — some may now fit within new capacity.
            self._cond.notify_all()

    def reset(self, initial_replicas: int = 2) -> None:
        with self._cond:
            self.replicas = initial_replicas
            self._active = 0
            self._total = 0
            self._failed = 0
            self._latency_window.clear()
            self._cond.notify_all()

    def snapshot(self) -> dict:
        with self._lock:
            lats = list(self._latency_window)
        if lats:
            import numpy as np  # noqa: PLC0415
            arr = np.array(lats)
            p50, p95, p99 = float(np.percentile(arr, 50)), float(np.percentile(arr, 95)), float(np.percentile(arr, 99))
            mean_lat = float(arr.mean())
        else:
            p50 = p95 = p99 = mean_lat = 0.0
        with self._lock:
            active = self._active
            total = self._total
            failed = self._failed
            reps = self.replicas
            cap = self.capacity
        return {
            "replicas": reps,
            "capacity": cap,
            "active_requests": active,
            "utilization_pct": round(min(100.0, active / max(cap, 1) * 100), 2),
            "total_requests": total,
            "failed_requests": failed,
            "mean_latency_ms": round(mean_lat, 2),
            "p50_latency_ms": round(p50, 2),
            "p95_latency_ms": round(p95, 2),
            "p99_latency_ms": round(p99, 2),
        }


state = ServiceState(initial_replicas=2)
app = Flask(__name__)

# ── Request handler ────────────────────────────────────────────────────────────

@app.route("/process")
def process():
    t_arrive = time.monotonic()
    acquired = state.acquire(timeout=5.0)
    t_got_worker = time.monotonic()
    queue_wait_ms = (t_got_worker - t_arrive) * 1000.0

    if not acquired:
        return jsonify({"error": "overloaded", "queue_wait_ms": round(queue_wait_ms, 2)}), 503

    try:
        # Simulate service work (CPU + sleep).
        # sqrt loop ensures *some* real CPU work; sleep models I/O / actual work duration.
        jitter = state._rng.gauss(0, BASE_JITTER_MS)
        service_ms = max(1.0, BASE_LATENCY_MS + jitter)
        _ = sum(math.sqrt(i) for i in range(2000))   # lightweight real CPU work
        time.sleep(service_ms / 1000.0)

        total_ms = (time.monotonic() - t_arrive) * 1000.0
        state.release(total_ms)
        return jsonify({
            "status": "ok",
            "latency_ms": round(total_ms, 2),
            "queue_wait_ms": round(queue_wait_ms, 2),
            "service_ms": round(service_ms, 2),
        }), 200
    except Exception as exc:  # noqa: BLE001
        state.release(0.0)
        return jsonify({"error": str(exc)}), 500


# ── Control endpoints ──────────────────────────────────────────────────────────

@app.route("/control/replicas/<int:n>", methods=["PUT"])
def set_replicas(n: int):
    state.set_replicas(n)
    snap = state.snapshot()
    return jsonify({"ok": True, "replicas": snap["replicas"], "capacity": snap["capacity"]}), 200


@app.route("/control/reset", methods=["POST"])
def reset():
    initial = int(request.json.get("initial_replicas", 2)) if request.is_json else 2
    state.reset(initial_replicas=initial)
    return jsonify({"ok": True, "replicas": state.replicas}), 200


@app.route("/metrics")
def metrics():
    return jsonify(state.snapshot()), 200


@app.route("/health")
def health():
    return jsonify({"status": "ok", "replicas": state.replicas}), 200


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
    print(f"[target_app] starting on port {port}, "
          f"THREADS_PER_REPLICA={THREADS_PER_REPLICA}, "
          f"BASE_LATENCY_MS={BASE_LATENCY_MS}")
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
