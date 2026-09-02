# Reference Sample API

A minimal, zero-dependency Python service built to demonstrate Kubernetes probe semantics, lifecycle transitions, and failure modes.

This application exists **solely to make probe behavior reproducible**. It provides distinct HTTP endpoints for startup, readiness, liveness, and user traffic, with controllable environment variables to simulate production failure scenarios.

---

## Endpoint Semantics

| Endpoint | Probe Type | Semantic Question | Failure Consequence in Kubernetes |
| :--- | :--- | :--- | :--- |
| `/startup` | `startupProbe` | *"Has initialization / cache loading completed?"* | Disables liveness checks until passed. If threshold exceeded, kubelet restarts the container. |
| `/ready` | `readinessProbe` | *"Should this instance receive traffic right now?"* | Marks the corresponding endpoint as not ready (`ready: false`). Normal Kubernetes Service traffic does not select it as a ready backend. Pod remains running; **no restart**. |
| `/health` | `livenessProbe` | *"Is the process alive enough that restarting will fix it?"* | Kubelet terminates the container and restarts it according to the `restartPolicy`. |
| `/` | Application Traffic | *"Standard API request payload"* | Returns `200 OK` when ready; returns `503 Service Unavailable` if hit while unready. |

> [!IMPORTANT]
> The three probe endpoints have intentionally separate logic. They are **not** aliases for each other:
> - A database being down should cause `/ready` to return 503 (stop sending traffic), while `/health` continues returning 200 (restarting the pod will not fix the database).
> - An application warming up caches should cause `/startup` to return 503, shielding it from premature `/health` evaluation.

---

## Simulation Controls (Demonstration Only)

The application supports environment variables to reproduce failure scenarios deterministically:

- `STARTUP_DELAY_SECONDS`: (float/int, default: `0`) Simulates slow initialization, schema migrations, or heavy cache warm-up.
- `SIMULATE_UNREADY`: (`true` / `false`, default: `false`) Simulates transient downstream degradation, thread-pool exhaustion, or graceful draining.
- `SIMULATE_LIVENESS_FAILURE`: (`true` / `false`, default: `false`) Simulates internal deadlock or unrecoverable process freeze.

> [!WARNING]
> Simulation controls exist strictly for failure scenario verification in controlled environments. Never expose arbitrary simulation flags in production workloads without proper authorization boundaries.

---

## Local Execution

### Run Directly with Python
```bash
# Standard execution
python app.py

# Automated probe self-test
python app.py --test-mode

# Simulate slow startup (15 seconds)
STARTUP_DELAY_SECONDS=15 python app.py
```

### Build & Run Container
```bash
docker build -t sample-api:1.0.0 .
docker run -p 8080:8080 sample-api:1.0.0
```
