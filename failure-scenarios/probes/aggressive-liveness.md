# Failure Scenario B: Aggressive Liveness as an Outage Amplifier

## Failure Chain

```text
Temporary application slowdown / downstream latency spike
        ↓
Liveness probe exceeds timeoutSeconds (or period * failureThreshold)
        ↓
Kubelet issues SIGTERM/SIGKILL (Container restart)
        ↓
Container cold start / initialization / cache rebuild
        ↓
Fresh container immediately saturated by queued traffic
        ↓
Liveness probe fails again during warm-up
        ↓
Another container restart (CrashLoopBackOff cascade)
        ↓
Total service collapse (Health mechanism becomes the primary outage cause)
```

---

## 1. Symptom

Under moderate to heavy traffic, a previously stable microservice experiences a rapid increase in container restarts across all replicas (`RESTARTS` count increments rapidly). 

User availability drops from 99.9% to 0%, and client-facing error rates reach 100% as pods flap between `Running`, `CrashLoopBackOff`, and terminating states.

---

## 2. Failure Domain

**Boundary:** Boundary between **Application Performance / Latency** and **Process Liveness Evaluation**.

The application process was not deadlocked; it was simply saturated, processing high-latency queries, or undergoing CPU throttling. By misinterpreting *slowness* as *death*, the cluster management layer turned a degradable performance event into total availability loss.

---

## 3. Evidence

### Pod Restart Activity
Running `kubectl get pods` shows rapid restart counts and short uptimes:
```text
# Illustrative example
NAME                          READY   STATUS             RESTARTS      AGE
sample-api-85947db5d9-4jh9q   0/1     CrashLoopBackOff   7 (90s ago)   14m
sample-api-85947db5d9-h8m2k   0/1     CrashLoopBackOff   6 (45s ago)   12m
```

### Kubelet Events
Describing the pod reveals `Unhealthy: Liveness probe failed` followed by container termination:
```text
# Illustrative example
Events:
  Type     Reason     Age                  From               Message
  ----     ------     ----                 ----               -------
  Warning  Unhealthy  62s (x3 over 72s)    kubelet            Liveness probe failed: HTTP probe failed with statuscode: 500
  Warning  Unhealthy  42s (x2 over 52s)    kubelet            Liveness probe failed: Get "http://10.244.1.42:8080/health": context deadline exceeded (Client.Timeout exceeded while awaiting headers)
  Normal   Killing    42s                  kubelet            Container sample-api failed liveness probe, will be restarted
```

---

## 4. Root Cause

### The "Obvious Fix" Fallacy
When an incident occurs due to an unresponsive container, engineers frequently respond with:
> *"Let's make the liveness probe check more aggressive (lower timeout, shorter period, fewer retries) so it recovers instantly when a pod hangs."*

For example:
```yaml
# HIGH RISK PATTERN: Overly aggressive liveness
livenessProbe:
  httpGet:
    path: /health
    port: 8080
  timeoutSeconds: 1     # 1 second timeout!
  periodSeconds: 2      # Checked every 2 seconds
  failureThreshold: 2   # Killed after only 4 seconds of slowness
```

### Why This Fails Catastrophically
When a burst of traffic or a downstream dependency delay occurs:
1. Application worker threads or the event loop become busy servicing client requests.
2. The probe HTTP request queues behind existing work.
3. Because `timeoutSeconds: 1` is tight, the probe times out even though the process is healthy and actively completing work.
4. Kubelet forcefully terminates the container.
5. All in-flight requests on that replica are abruptly aborted (502/504 errors).
6. Traffic that was on the killed replica redistributes to the remaining replicas, immediately pushing them over the same cliff.
7. As pods restart, they must re-warm caches and re-establish DB connections, which is CPU-intensive. Saturated nodes throttle them, causing liveness to fail repeatedly.

---

## 5. Recovery & Calibration Guidelines

### Immediate Mitigation
Patch the deployment to relax or temporarily disable the liveness probe failure threshold while the cluster stabilizes:
```bash
kubectl patch deployment sample-api -n sample-workloads --type='json' \
  -p='[{"op": "replace", "path": "/spec/template/spec/containers/0/livenessProbe/failureThreshold", "value": 10}]'
```

### Strategic Calibration

There are **no universal magic values** for probe timings; values depend directly on application architecture, garbage collection characteristics, and initialization profiles. However, production designs must adhere to the following principles:

1. **Decouple Liveness from Downstream Health:**
   The `/health` endpoint must evaluate internal process state only (is the event loop ticking? are background consumer threads running?). Never query a database or external cache in a liveness probe.

2. **Absorb Startup with `startupProbe`:**
   Do not inflate `initialDelaySeconds` on liveness to accommodate worst-case cold starts. Use a `startupProbe` with a high `failureThreshold` (e.g., 30 checks every 2s = 60s window). This gives slow-starting pods headroom without sacrificing fast detection of deadlocks in steady state.

3. **Provide Latency Headroom for Liveness:**
   `livenessProbe` should be deliberately patient:
   - `timeoutSeconds`: Allow at least 2–5 seconds to survive short CPU spikes or minor GC pauses.
   - `periodSeconds`: 10–15 seconds is often sufficient. Liveness is an emergency fallback for deadlocks, not an instant routing toggle.
   - `failureThreshold`: 3 or more consecutive failures (so a single transient timeout does not trigger a restart).

4. **Use `readinessProbe` for Traffic Shedding:**
   If a container is overloaded, let `readinessProbe` fail to remove it from the Service endpoint list. The container stops receiving new requests, cools down, finishes its current backlog, and becomes ready again—**without losing its cache or restarting PID 1**.
