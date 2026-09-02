#!/usr/bin/env python3
"""
Production EKS Platform - Reference Sample API
Demonstrating Kubernetes Probe Semantics (Running != Ready)

Endpoints:
- /startup : Initialization state check (protects slow initialization)
- /ready   : Traffic eligibility check (controls EndpointSlice participation)
- /health  : Process liveness check (determines if restart recovers the process)
- /        : Application business payload (returns 503 if not ready)

Simulation Controls (Demonstration only):
- STARTUP_DELAY_SECONDS: Float/int seconds to simulate heavy initial cache/warmup
- SIMULATE_UNREADY: "true" to simulate transient downstream degradation
- SIMULATE_LIVENESS_FAILURE: "true" to simulate fatal deadlock or unrecoverable state
"""

import http.server
import json
import os
import sys
import time
import urllib.request
import threading

PORT = int(os.environ.get("PORT", "8080"))
STARTUP_DELAY_SECONDS = float(os.environ.get("STARTUP_DELAY_SECONDS", "0"))
SIMULATE_UNREADY = os.environ.get("SIMULATE_UNREADY", "false").lower() == "true"
SIMULATE_LIVENESS_FAILURE = os.environ.get("SIMULATE_LIVENESS_FAILURE", "false").lower() == "true"

PROCESS_START_TIME = time.time()


class HealthProbeHandler(http.server.BaseHTTPRequestHandler):
    def send_json(self, status_code: int, payload: dict):
        response_data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_data)))
        self.end_headers()
        self.wfile.write(response_data)

    def log_message(self, format, *args):
        # Structured log line format
        sys.stdout.write(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}\n")
        sys.stdout.flush()

    def is_startup_complete(self) -> tuple[bool, float]:
        elapsed = time.time() - PROCESS_START_TIME
        return (elapsed >= STARTUP_DELAY_SECONDS), elapsed

    def do_GET(self):
        startup_done, elapsed = self.is_startup_complete()

        # ----------------------------------------------------------------------
        # 1. /startup: Has initialization completed?
        # ----------------------------------------------------------------------
        if self.path == "/startup":
            if not startup_done:
                self.send_json(503, {
                    "probe": "startup",
                    "status": "INITIALIZING",
                    "ready": False,
                    "elapsed_seconds": round(elapsed, 2),
                    "required_warmup_seconds": STARTUP_DELAY_SECONDS,
                    "message": "Application initialization or cache warm-up in progress"
                })
            else:
                self.send_json(200, {
                    "probe": "startup",
                    "status": "INITIALIZED",
                    "ready": True,
                    "elapsed_seconds": round(elapsed, 2),
                    "message": "Application initialization completed successfully"
                })
            return

        # ----------------------------------------------------------------------
        # 2. /ready: Should this instance receive user traffic right now?
        # ----------------------------------------------------------------------
        if self.path == "/ready":
            if not startup_done:
                self.send_json(503, {
                    "probe": "readiness",
                    "status": "NOT_READY",
                    "eligible_for_traffic": False,
                    "reason": "startup_in_progress",
                    "message": "Cannot accept traffic while startup initialization is underway"
                })
            elif SIMULATE_UNREADY:
                self.send_json(503, {
                    "probe": "readiness",
                    "status": "NOT_READY",
                    "eligible_for_traffic": False,
                    "reason": "simulated_degradation",
                    "message": "Temporarily unable to serve traffic (e.g. queue saturated or downstream degraded). Do NOT restart."
                })
            else:
                self.send_json(200, {
                    "probe": "readiness",
                    "status": "READY",
                    "eligible_for_traffic": True,
                    "message": "Instance healthy and actively eligible for Service endpoint traffic"
                })
            return

        # ----------------------------------------------------------------------
        # 3. /health: Is process alive enough that restarting is appropriate?
        # ----------------------------------------------------------------------
        if self.path == "/health":
            if SIMULATE_LIVENESS_FAILURE:
                self.send_json(500, {
                    "probe": "liveness",
                    "status": "DEADLOCK_OR_FATAL",
                    "action_required": "restart_container",
                    "message": "Fatal deadlock simulated. Kubelet container restart is the correct recovery action."
                })
            else:
                self.send_json(200, {
                    "probe": "liveness",
                    "status": "ALIVE",
                    "process_alive": True,
                    "uptime_seconds": round(time.time() - PROCESS_START_TIME, 2),
                    "message": "Process execution loop is responsive. Do not restart."
                })
            return

        # ----------------------------------------------------------------------
        # 4. / (Application Traffic Endpoint)
        # ----------------------------------------------------------------------
        if self.path == "/" or self.path == "/api/v1/workload":
            # If the application is not ready, receiving traffic triggers a 503
            if not startup_done or SIMULATE_UNREADY:
                self.send_json(503, {
                    "error": "Service Unavailable",
                    "detail": "Application process is running, but internal state is not ready to serve traffic.",
                    "code": 503
                })
            else:
                self.send_json(200, {
                    "status": "OK",
                    "workload": "sample-api",
                    "version": "1.0.0",
                    "uptime_seconds": round(time.time() - PROCESS_START_TIME, 2)
                })
            return

        # Fallback for unknown paths
        self.send_json(404, {"error": "Not Found", "path": self.path})


def run_server():
    server_address = ("", PORT)
    httpd = http.server.HTTPServer(server_address, HealthProbeHandler)
    print(f"Starting sample-api server on port {PORT} (STARTUP_DELAY={STARTUP_DELAY_SECONDS}s, SIMULATE_UNREADY={SIMULATE_UNREADY}, SIMULATE_LIVENESS_FAILURE={SIMULATE_LIVENESS_FAILURE})...")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()


def run_self_test():
    """Execute automated self-test verification covering normal and failure simulation semantics."""
    global STARTUP_DELAY_SECONDS, SIMULATE_UNREADY, SIMULATE_LIVENESS_FAILURE

    print("Running automated probe semantics self-test...")
    server = http.server.HTTPServer(("127.0.0.1", 18080), HealthProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)

    base = "http://127.0.0.1:18080"

    def query(path):
        req = urllib.request.Request(f"{base}{path}")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.getcode(), json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    try:
        # 1. Baseline Ready Operation
        print("  [Test 1/4] Baseline Ready State:")
        for ep in ["/startup", "/ready", "/health", "/"]:
            code, body = query(ep)
            assert code == 200, f"Expected 200 for {ep}, got {code}"
            print(f"    PASS: GET {ep} -> {code} ({body.get('status') or body.get('message')})")

        # 2. Startup Delay Simulation
        print("  [Test 2/4] Startup Delay Simulation:")
        STARTUP_DELAY_SECONDS = 9999.0
        code, _ = query("/startup")
        assert code == 503, f"Expected 503 on /startup during delay, got {code}"
        code, _ = query("/ready")
        assert code == 503, f"Expected 503 on /ready during startup delay, got {code}"
        code, _ = query("/health")
        assert code == 200, f"Expected 200 on /health during startup delay, got {code}"
        code, _ = query("/")
        assert code == 503, f"Expected 503 on / during startup delay, got {code}"
        print("    PASS: /startup=503, /ready=503, /health=200, /=503 verified during slow startup")
        STARTUP_DELAY_SECONDS = 0.0

        # 3. Transient Unreadiness Simulation (Traffic Shedding / Downstream degradation)
        print("  [Test 3/4] Transient Unreadiness Simulation:")
        SIMULATE_UNREADY = True
        code, _ = query("/startup")
        assert code == 200, f"Expected 200 on /startup, got {code}"
        code, body = query("/ready")
        assert code == 503 and body.get("reason") == "simulated_degradation", f"Expected 503 on /ready, got {code}"
        code, _ = query("/health")
        assert code == 200, f"Expected 200 on /health (process alive), got {code}"
        code, _ = query("/")
        assert code == 503, f"Expected 503 on / while unready, got {code}"
        print("    PASS: /ready=503 while /health=200 (traffic shed, no restart triggered)")
        SIMULATE_UNREADY = False

        # 4. Liveness Deadlock Failure Simulation
        print("  [Test 4/4] Liveness Deadlock Failure Simulation:")
        SIMULATE_LIVENESS_FAILURE = True
        code, body = query("/health")
        assert code == 500 and body.get("status") == "DEADLOCK_OR_FATAL", f"Expected 500 on /health, got {code}"
        print("    PASS: /health=500 (correctly signals restart recovery action)")
        SIMULATE_LIVENESS_FAILURE = False

        print("All probe semantics and failure simulation tests passed successfully.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    if "--test-mode" in sys.argv:
        run_self_test()
    else:
        run_server()

