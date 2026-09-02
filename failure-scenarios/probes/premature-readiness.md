# Failure Scenario A: Premature Readiness (Running ≠ Ready)

## Failure Chain

```text
Container starts
        ↓
Process becomes Running
        ↓
Initialization remains incomplete
        ↓
Incorrect readiness succeeds
        ↓
Pod becomes traffic eligible (endpoint marked ready in EndpointSlice)
        ↓
Client requests arrive
        ↓
Application cannot serve correctly (cache unpopulated / connection pool unready)
        ↓
HTTP 5xx / connection refused failures for users
```

---

## 1. Symptom

During a deployment rollout or horizontal pod autoscaling (HPA) scale-out event, users immediately experience a sharp spike in HTTP 502 Bad Gateway or HTTP 503 Service Unavailable errors. 

Simultaneously, the operations dashboard indicates that all new Pods are in the `Running` phase, and the Deployment rollout reports successful completion.

---

## 2. Failure Domain

**Boundary:** Boundary between **Container Process Lifecycle** and **Application Serving Readiness**.

The Kubernetes control plane correctly observes that the container process (PID 1) started and the socket responded. However, the application layer inside that container has not completed internal warm-up, configuration fetching, or connection pool establishment.

---

## 3. Evidence

Operators investigating the cluster observe conflicting indicators:

### Pod Status vs. User Traffic
`kubectl get pods` shows status `Running`:
```text
# Illustrative example
NAME                          READY   STATUS    RESTARTS   AGE
sample-api-7c4d57b448-k7x8v   1/1     Running   0          45s
sample-api-7c4d57b448-wz2p9   1/1     Running   0          45s
```

### Ingress / Load Balancer Access Logs
Client requests hitting the newly scheduled Pod IPs return HTTP 503:
```text
# Illustrative example
2026-09-02T23:30:15Z client=198.51.100.4 upstream=10.244.1.42:8080 status=503 request="GET /api/v1/workload HTTP/1.1" response_time=0.002
2026-09-02T23:30:16Z client=198.51.100.7 upstream=10.244.1.43:8080 status=503 request="GET /api/v1/workload HTTP/1.1" response_time=0.003
```

### Application Logs
Application logs from the newly started pods show that initialization was still underway when requests landed:
```text
# Illustrative example
[2026-09-02 23:30:14] [sample-api] Server bound to port 8080
[2026-09-02 23:30:15] [sample-api] 10.244.0.1 "GET / HTTP/1.1" 503 - Application not ready (cache warmup in progress: 12% complete)
[2026-09-02 23:30:16] [sample-api] 10.244.0.1 "GET / HTTP/1.1" 503 - Application not ready (cache warmup in progress: 34% complete)
[2026-09-02 23:30:28] [sample-api] Cache warm-up completed successfully. Ready to serve.
```

---

## 4. Root Cause

The readiness probe was either:
1. **Omitted entirely**, causing Kubernetes to default to treating the Pod as `Ready` the instant PID 1 starts and the container is in the `Running` phase.
2. **Checking a shallow endpoint** (such as a generic TCP socket check or a static `/` that returns HTTP 200 before dependent subsystems are initialized).
3. **Checking an alias for liveness** rather than validating actual request-processing readiness.

Because the readiness condition was evaluated as `True` prematurely, the endpoint was marked ready in the EndpointSlice, allowing Service traffic to route to the Pod before the application could process user transactions.

---

## 5. Recovery

### Immediate Mitigation
- Set `minReadySeconds` on the Deployment specification to introduce a mandatory buffer after a pod becomes ready before old pods are terminated.
- Revert rollout to the prior stable revision if the new pods cannot stabilize:
  ```bash
  kubectl rollout undo deployment/sample-api -n sample-workloads
  ```

### Long-Term Architectural Fix
1. Implement a **dedicated `/ready` endpoint** that explicitly verifies critical operational preconditions (connection pools connected, cache warmed, route table compiled).
2. Couple the deployment with a **`startupProbe`** on `/startup` to prevent any traffic consideration until initialization signals full completion.
3. Configure appropriate `initialDelaySeconds`, `failureThreshold`, and `periodSeconds` on `readinessProbe` to reflect realistic application warm-up timings.
