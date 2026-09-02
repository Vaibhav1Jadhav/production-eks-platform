# Runbook: Pod Running but Users Receive 5xx

## Incident Overview

- **Severity:** High / Availability Degrading
- **Symptom:** End users or upstream clients receive HTTP 502 Bad Gateway, 503 Service Unavailable, or 504 Gateway Timeout errors when communicating with a service, yet `kubectl get pods` displays all workload pods in the `Running` state.
- **Goal:** Systematically isolate whether the issue originates in the external gateway, Service discovery layer, EndpointSlice synchronization, readiness probe evaluation, application initialization, or downstream dependencies.

---

## Outside-In Diagnostic Flow

When troubleshooting traffic failure against running pods, work systematically from the external client inward to the internal process:

```text
Client / Consumer
        ↓
Gateway / Ingress / Cloud Load Balancer
        ↓
Kubernetes Service (ClusterIP / Port mapping)
        ↓
EndpointSlice (Active pod IP registration & Ready status)
        ↓
Pod Conditions (PodReady vs. ContainersReady)
        ↓
Kubelet Probe Behavior (Readiness / Startup probe responses)
        ↓
Application Runtime (Logs, internal state, connection pools)
        ↓
Downstream Dependencies (Databases, caches, third-party APIs)
```

---

## Step-by-Step Diagnostic Investigation

### Step 1: Verify External Gateway vs. Cluster Ingress
- **Hypothesis:** The failure is occurring upstream of the Kubernetes cluster (e.g., DNS misconfiguration, edge WAF block, or Load Balancer listener mismatch).
- **Command / Evidence:**
  ```bash
  # Test the service directly from an in-cluster debug pod to bypass the edge load balancer
  kubectl run curl-debug --image=curlimages/curl --rm -it --restart=Never \
    -n sample-workloads -- curl -iv http://sample-api.sample-workloads.svc.cluster.local:80/
  ```
- **Interpretation:**
  - If the in-cluster curl returns `200 OK`: The issue is in the Ingress/Gateway or external Load Balancer routing configuration.
  - If the in-cluster curl returns `502` or `503`: The failure is internal to the cluster service or workload.
- **Next Step:** If internal, proceed to Step 2 to inspect Service definition and EndpointSlice state.

---

### Step 2: Inspect Kubernetes Service & EndpointSlice Registration
- **Hypothesis:** The Service selector does not match pod labels, or the EndpointSlice controller has excluded the pods because they are marked unready.
- **Command / Evidence:**
  ```bash
  # Check Service definition and selector
  kubectl get service sample-api -n sample-workloads -o wide

  # Inspect active EndpointSlices for the service
  kubectl get endpointslice -l kubernetes.io/service-name=sample-api -n sample-workloads -o wide
  ```
- **Interpretation:**
  - If `ENDPOINTS` in EndpointSlice is empty or `<none>`: The Service selector is mismatched OR all matching pods have failed readiness checks.
  - If EndpointSlice shows pod IPs with `READY=false`: Pods are running, but Kubernetes intentionally refuses to route traffic to them due to readiness probe failure.
  - If EndpointSlice shows pod IPs with `READY=true`, but traffic still fails with 5xx: The application itself is receiving traffic and explicitly returning 5xx errors (premature readiness or internal application fault).
- **Next Step:** Inspect Pod status conditions in Step 3.

---

### Step 3: Inspect Pod Status Conditions & Probes
- **Hypothesis:** The container process is executing, but the pod's `Ready` condition is `False`, or the container passed readiness prematurely.
- **Command / Evidence:**
  ```bash
  # Inspect detailed pod conditions
  kubectl get pods -n sample-workloads -o custom-columns=\
NAME:.metadata.name,\
STATUS:.status.phase,\
READY:.status.containerStatuses[0].ready,\
STARTED:.status.containerStatuses[0].started,\
RESTARTS:.status.containerStatuses[0].restartCount

  # View exact condition transition reasons
  kubectl describe pod <pod-name> -n sample-workloads
  ```
- **Interpretation:**
  - Look at the `Conditions` section in `describe`:
    - `ContainersReady: False` + `Ready: False`: Container is running, but readiness probe has not succeeded.
    - `ContainersReady: True` + `Ready: True`: Kubernetes considers the pod healthy; traffic IS being delivered to the container.
  - Look at the `Events` section:
    - Look for `Warning Unhealthy Readiness probe failed: HTTP probe failed with statuscode: 503`.
    - Look for `Warning Unhealthy Startup probe failed: ...`.
- **Next Step:** If readiness probe is failing, proceed to Step 4. If readiness passed but users get 5xx, proceed to Step 5.

---

### Step 4: Validate Local Probe Endpoints Manually
- **Hypothesis:** The probe endpoint is returning a non-2xx status code or timing out.
- **Command / Evidence:**
  ```bash
  # Port forward directly to the pod's container port to bypass Services and EndpointSlices
  kubectl port-forward pod/<pod-name> -n sample-workloads 8080:8080

  # In another terminal, query each probe endpoint individually
  curl -i http://localhost:8080/startup
  curl -i http://localhost:8080/ready
  curl -i http://localhost:8080/health
  curl -i http://localhost:8080/
  ```
- **Interpretation:**
  - If `/startup` returns `503`: The application is still initializing. Check if `failureThreshold` or `initialDelaySeconds` on `startupProbe` is sufficient.
  - If `/ready` returns `503` while `/health` returns `200`: The application process is healthy, but an internal dependency (e.g. database, queue) is unavailable or connection pool is exhausted. **Do not restart the container.**
  - If `/` returns `503` while `/ready` returns `200`: **Premature Readiness Bug**. The application `/ready` check is shallow and claimed readiness before the handler could process business logic.
- **Next Step:** Inspect application logs in Step 5.

---

### Step 5: Analyze Application Runtime Logs
- **Hypothesis:** The application is logging uncaught exceptions, timeout errors, or refusing connections due to resource exhaustion or missing configuration.
- **Command / Evidence:**
  ```bash
  # Stream current logs
  kubectl logs -n sample-workloads deployment/sample-api --tail=100 -f

  # Inspect logs from previously crashed instances (if restarts occurred)
  kubectl logs -n sample-workloads <pod-name> --previous
  ```
- **Interpretation:**
  - Look for error signatures: database connection timeouts, memory limit warnings, unhandled promise rejections, thread deadlocks.
- **Next Step:** Execute targeted recovery in Step 6.

---

## Recovery Procedures

### Scenario 1: Pods Failing Readiness Due to Transient Dependency Outage
- **Action:**
  1. Do NOT kill the application pods. Mass container restarts will not resolve an external database or network outage.
  2. Verify downstream dependency health (database connection limits, network policies, IAM permissions).
  3. Once the dependency recovers, the application's `/ready` probe will automatically return `200 OK`, and the EndpointSlice controller will restore the pod IPs into service with zero manual intervention.

### Scenario 2: Rollout Introduced Premature Readiness (5xx During Deployment)
- **Action:**
  1. Immediately roll back the deployment to the previous stable revision:
     ```bash
     kubectl rollout undo deployment/sample-api -n sample-workloads
     ```
  2. Update the application's `/ready` check to verify true functional readiness before returning HTTP 200.
  3. Ensure `minReadySeconds: 15` is configured in the Deployment spec to guarantee pods soak in production before older replicas terminate.

### Scenario 3: Aggressive Liveness Probe Causing Restart Storm
- **Action:**
  1. Patch the deployment to increase the liveness failure threshold and timeout to grant breathing room:
     ```bash
     kubectl patch deployment sample-api -n sample-workloads --type='json' \
       -p='[{"op": "replace", "path": "/spec/template/spec/containers/0/livenessProbe/timeoutSeconds", "value": 5}, {"op": "replace", "path": "/spec/template/spec/containers/0/livenessProbe/failureThreshold", "value": 6}]'
     ```
  2. If slow initialization is triggering restarts, add or extend `startupProbe`.
