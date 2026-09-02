# ADR-001: Kubernetes Health Probe Strategy

## Status

Accepted

---

## Context

In Kubernetes, container lifecycle state does not automatically equal application service readiness:

$$\text{Container Running} \ne \text{Application Ready}$$

A container process can transition to the `Running` state immediately after execution begins, but the internal application may still be loading data, establishing database connection pools, compiling bytecode, or warming internal caches. Conversely, a running container may experience transient upstream dependency delays or queue saturation while its core process execution loop remains healthy.

A naive or unified health probe strategy introduces severe production failure modes:

1. **Premature Traffic Injection:** If readiness is checked via process existence or a generic `/health` check that returns `200` prematurely, the Service routes user requests before the application can process them, triggering HTTP 5xx spikes during deployments or rollouts.
2. **Aggressive Restart Storms (Outage Cascades):** If `livenessProbe` checks downstream dependencies (e.g., querying the database or Redis cache), a transient dependency outage causes every application replica to fail liveness simultaneously. The kubelet terminates and restarts all pods concurrently, wiping internal memory caches, overwhelming the database on restart, and converting a minor dependency brownout into a total cluster outage.
3. **Flapping & Death Spirals:** Tight probe timeouts coupled with slow initial warm-ups cause containers to be killed before completing initialization, trapping workloads in continuous `CrashLoopBackOff` cycles.

---

## Decision

We adopt **distinct, isolated semantics** for all three Kubernetes probe mechanisms across all production-oriented workloads:

1. **`startupProbe` — Initialization Boundary:**
   - **Target:** Dedicated `/startup` endpoint.
   - **Responsibility:** Confirms that application bootstrapping, configuration loading, schema verification, and essential cache warming are complete.
   - **Behavior:** Completely disables liveness and readiness evaluation during the startup window. Provides a generous failure threshold to absorb variable cold starts without risking process restart.

2. **`readinessProbe` — Traffic Eligibility Boundary:**
   - **Target:** Dedicated `/ready` endpoint.
   - **Responsibility:** Determines whether this specific replica can successfully process incoming client traffic *right now*.
   - **Behavior:** Readiness failure marks the corresponding endpoint as not ready (`ready: false`). Normal Kubernetes Service traffic does not select it as a ready backend. The container **is never restarted** as a result of readiness failure. Checks local saturation (e.g., worker pool availability, circuit breakers).

3. **`livenessProbe` — Deadlock Recovery Boundary:**
   - **Target:** Dedicated `/health` endpoint.
   - **Responsibility:** Determines whether the process runtime has entered an unrecoverable state (e.g., deadlock, stuck thread pool, fatal internal invariant violation) where **killing and restarting the container is the only viable recovery mechanism**.
   - **Constraint:** **Liveness should generally avoid depending on shared downstream services whose failure cannot be repaired by restarting this container.** Coupling liveness to shared external dependencies (databases, third-party APIs, shared caches) risks triggering simultaneous container restarts across all replicas during a dependency outage, amplifying downtime into a cascading cluster failure. Liveness evaluates local process execution health.

---

## Alternatives Considered

| Alternative | Technical Evaluation | Verdict |
| :--- | :--- | :--- |
| **1. Single Generic `/health` Endpoint for All Probes** | Couples traffic routing and restart triggers together. If the application slows down or warms up, the kubelet kills it; if an external dependency falters, all pods restart simultaneously. | **Rejected** (Violates fault isolation) |
| **2. Liveness Tied to Downstream Dependencies** | A database blip causes all application pods to report unhealthy to liveness. Every pod restarts at once, amplifying the downstream load during restart and causing cascading collapse. | **Rejected** (Catastrophic failure amplifier) |
| **3. Readiness Only (No Liveness)** | Prevents restart loops during degradation, but leaves containers that suffer unrecoverable deadlocks or thread pool freezes stranded indefinitely without automatic healing. | **Rejected** (Requires manual intervention for fatal deadlocks) |
| **4. Separate Probe Semantics (`startup`, `ready`, `health`)** | Decouples startup absorption from steady-state monitoring; separates traffic removal from destructive process restarts. | **Accepted** (Enforces clean failure boundaries) |

---

## Consequences

### Positive
- **Safer Rolling Deployments:** New Pods become traffic-eligible only after readiness succeeds, reducing the risk of premature traffic during rollout (probes alone do not guarantee zero downtime, which also requires graceful termination and budget controls).
- **Cascading Failure Protection:** Downstream database degradation causes pods to fail readiness (shedding load or serving degraded fallback) rather than triggering mass container restarts.
- **Predictable Cold-Start Handling:** Heavy initialization does not require artificially inflating steady-state liveness probe timeouts.

### Negative / Costs
- **Application Engineering Overhead:** Developers must build and maintain three separate endpoints with distinct semantic rules rather than exposing a single ping endpoint.
- **Configuration Complexity:** Operators must calibrate timeouts, periods, and failure thresholds across three distinct probe configurations per container.

---

## Failure Boundaries

What this probe strategy **can** protect against:
- Premature traffic delivery during rolling updates or autoscaling scale-out events.
- Restart storms resulting from transient shared dependency outages.
- Silent container deadlocks where the process is running but unresponsive.

What this probe strategy **cannot** protect against:
- Silent algorithmic data corruption where the HTTP probe endpoint continues to return 200 OK.
- Capacity exhaustion where 100% of replicas fail readiness due to traffic volume (traffic shedding must be managed via rate limiting, autoscaling, or graceful degradation).
- Edge-network failures upstream of the Kubernetes cluster (e.g., Ingress controller crashes, DNS resolution failures).

---

## Operational Evidence & Observability

When investigating probe-related behavior, operators must examine:

1. **Pod Status Conditions:**
   ```bash
   kubectl get pod <pod-name> -o jsonpath='{.status.conditions}'
   ```
   Inspect `ContainersReady` vs. `Ready` vs. `Initialized`.
2. **Kubelet Warning Events:**
   ```bash
   kubectl get events --field-selector involvedObject.name=<pod-name>
   ```
   Look for `Unhealthy: Readiness probe failed: ...` vs. `Unhealthy: Liveness probe failed: ... Killing container`.
3. **EndpointSlice Membership:**
   ```bash
   kubectl get endpointslice -l kubernetes.io/service-name=sample-api
   ```
   Verify whether Pod IPs are marked `ready: true` or `ready: false`.
