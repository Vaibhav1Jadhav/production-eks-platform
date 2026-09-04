# ADR-002: PodDisruptionBudget Strategy for Planned Maintenance

## Status

Accepted

---

## Context

In production Kubernetes clusters, workloads are subjected to continuous planned maintenance:
- Worker node operating system patches and security updates (e.g., automated AMI rotations in Amazon EKS).
- Cluster autoscaler (CAS / Karpenter) node consolidation and scale-down operations.
- Kubernetes minor and patch version upgrades requiring sequential node draining.

When an administrator or automated process drains a node via `kubectl drain`, the tool attempts to gracefully evict all pods resident on that node using the Kubernetes **Eviction API**.

Without explicit constraints, multiple nodes draining in parallel or rapid sequential drains can terminate too many replicas of a workload before replacement pods can initialize, pass readiness checks, and begin serving user traffic. This results in service brownouts, elevated latency, or outright 5xx outages during routine operational maintenance.

---

## Problem

A naive approach to workload availability assumes that setting a replica count (e.g., `spec.replicas: 3`) is sufficient to survive node-level disruptions. However:

1. **Uncontrolled Voluntary Evictions:** By default, Kubernetes does not restrict how many pods of a Deployment can be evicted simultaneously during node drains. If two nodes hosting two of the three replicas are drained concurrently, 66% of serving capacity vanishes instantly.
2. **The "PDB as High Availability" Myth:** Engineers frequently mistake a `PodDisruptionBudget` (PDB) for high availability. A PDB does **not** protect against involuntary crashes (OOM, kernel panics, hardware failures) and does **not** ensure pods are placed across separate failure domains or zones.
3. **Deadlocks from Over-Constrained Budgets:** Setting an over-restrictive budget (e.g., requiring 100% availability on an unscaled workload) causes the Eviction API to return HTTP 429 (`InsufficientPods`), permanently blocking automated node patching and cluster upgrades.
4. **Unhealthy Pod Maintenance Deadlocks:** If a replica is crash-looping or failing readiness checks due to an internal bug, a naive PDB can prevent cluster operators from draining the node hosting that broken pod, turning an application-level defect into an infrastructure maintenance freeze.

---

## Decision

We adopt a declarative **`policy/v1` PodDisruptionBudget** for the `sample-api` workload with the following parameters:

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: sample-api
  namespace: sample-workloads
spec:
  minAvailable: 2
  unhealthyPodEvictionPolicy: AlwaysAllow
  selector:
    matchLabels:
      app.kubernetes.io/name: sample-api
```

### Core Architecture Rationale:
1. **Workload Sizing Baseline:** The `sample-api` Deployment is scaled to **3 replicas**.
2. **Disruption Budget (`minAvailable: 2`):** 
   - Under steady-state conditions ($3\text{ ready pods}$), exactly **1 voluntary disruption** is permitted (`disruptionsAllowed = 1`).
   - If one pod is evicted, the remaining 2 pods preserve $66\%$ of serving capacity, comfortably absorbing baseline traffic while the replacement pod initializes.
   - While the replacement pod is initializing (warming caches via `startupProbe` and validating readiness via `readinessProbe`), `disruptionsAllowed` drops to **0**, blocking any further node drains that would evict the remaining replicas.
3. **Unhealthy Pod Eviction Policy (`AlwaysAllow`):**
   - We explicitly set `unhealthyPodEvictionPolicy: AlwaysAllow` (stable in Kubernetes v1.31+).
   - If a pod is already unhealthy (failing readiness or pending), it should **not** block node drains or host patching. The Eviction API will allow its eviction even if the healthy pod count is below `minAvailable`.

---

## Decision Drivers

- **Traffic Resilience During Maintenance:** Prevent routine node rotations from causing client-facing HTTP 503/504 errors.
- **Operational Non-Blocking:** Ensure automated cluster upgrades and node drains can proceed predictably without human intervention deadlocks.
- **Explicit Failure Semantics:** Clearly delineate between policy-governed voluntary evictions, direct pod deletions, controller rolling upgrades, and involuntary hardware crashes.

---

## Alternatives Evaluated

| Strategy | Technical Evaluation | Verdict |
| :--- | :--- | :--- |
| **A. No PDB (Default)** | Provides maximum operational flexibility for node draining, but offers zero protection for application capacity. Multiple node drains can terminate all replicas simultaneously. | **Rejected** (High risk of maintenance-induced downtime) |
| **B. `minAvailable: 1` (on 3 replicas)** | Permits 2 voluntary disruptions simultaneously. However, leaving only 1 replica online cuts serving capacity by $67\%$, causing queue saturation and request timeouts under peak traffic. | **Rejected** (Excessive capacity loss for a 3-replica tier) |
| **C. `minAvailable: 2` (on 3 replicas)** | Permits exactly 1 voluntary eviction while guaranteeing 2 ready replicas remain active. Drains proceed smoothly once replacement pods become ready. | **Accepted** (Calibrated balance for 3-replica reference workload) |
| **D. `minAvailable: 3` (or `100%`)** | Requires all 3 replicas to be healthy at all times. Because desired equals minimum, `disruptionsAllowed` is permanently `0`. Any `kubectl drain` is rejected with HTTP 429, deadlocking cluster maintenance. | **Rejected** (Guarantees maintenance deadlock) |
| **E. `maxUnavailable: 1` vs `minAvailable: 2`** | For a fixed 3-replica deployment, `maxUnavailable: 1` and `minAvailable: 2` yield identical disruption arithmetic. However, `minAvailable` explicitly states the minimum required serving floor, whereas `maxUnavailable` is often preferred when autoscaling (HPA) causes replica counts to fluctuate dynamically. | **Accepted for static sizing** (`minAvailable: 2` makes the minimum capacity guarantee explicit) |
| **F. `unhealthyPodEvictionPolicy: IfHealthyBudget`** | Unhealthy pods can only be evicted if the disruption budget is satisfied. If a workload has 1 broken pod and 2 healthy pods, draining the broken pod's node is blocked because available capacity would drop. This traps cluster upgrades behind application-level bugs. | **Rejected** (Allows broken applications to freeze infrastructure maintenance) |
| **G. `unhealthyPodEvictionPolicy: AlwaysAllow`** | Unhealthy pods are permitted to be evicted immediately. Host maintenance proceeds unimpeded. Healthy pods remain protected by `minAvailable`. | **Accepted** (Enforces infrastructure maintainability; stable since v1.31) |

---

## Consequences

### Positive
- **Guaranteed Serving Floor:** Under planned voluntary disruptions, at least 2 healthy replicas continue serving client traffic.
- **Automatic Drain Throttling:** Node drain automation automatically throttles to the speed of application warm-up and probe readiness.
- **Resilient Infrastructure Patching:** Unhealthy pods cannot trap node drains or delay critical OS/kernel security patching.

### Negative / Trade-offs
- **Drain Serialization Delay:** Node drains must wait for replacement pods to complete startup and readiness probes before the next node can be drained. Fast rolling reboots of worker nodes will take longer.
- **Overhead on Small Clusters:** If a cluster lacks spare capacity (e.g., no schedulable nodes), replacement pods remain `Pending`, causing subsequent drains to block indefinitely until capacity is added or timeouts trigger.
- **Operational Understanding Required:** SREs and platform engineers must understand that PDBs do not protect against node crashes or direct `kubectl delete pod` commands.

---

## Failure Boundaries & Non-Goals

1. **PDB Does Not Govern Deployment Rolling Updates:**
   - Workload rollout speed is controlled by the Deployment controller using `spec.strategy.rollingUpdate.maxUnavailable` and `maxSurge`.
   - The Deployment controller does **not** call the Eviction API during a rollout; therefore, PDB does not govern rolling upgrades.
2. **PDB Does Not Prevent Involuntary Disruption:**
   - A hardware failure, kernel panic, or sudden AWS spot instance interruption cannot be prevented by a PDB.
   - However, involuntary losses **consume the budget**, reducing `disruptionsAllowed` to `0` and preventing subsequent voluntary drains from proceeding until recovery occurs.
3. **Direct Deletion Bypasses PDB:**
   - Running `kubectl delete pod <name>` directly invokes the core Pod API, bypassing the Eviction API. PDB protections do not apply to direct pod deletion.

---

## Operational Evidence & Status Inspection

Engineers can inspect the real-time state of the disruption budget via the Kubernetes API:

```bash
kubectl get pdb sample-api -n sample-workloads -o wide
```

Expected output fields:
- `MIN AVAILABLE`: Desired minimum ready replicas (`2`).
- `MAX UNAVAILABLE`: N/A (when `minAvailable` is used).
- `ALLOWED DISRUPTIONS`: Number of voluntary evictions currently permitted (`1` when 3 ready, `0` during eviction/warmup).
- `CURRENT HEALTHY`: Number of matching pods currently in `Ready: True` condition.
- `DESIRED HEALTHY`: Minimum number of healthy pods required by the budget.

---

## Relationship to Milestone #21 & Milestone #23

- **Milestone #21 (Workload Probes):** Establishes the **Readiness Boundary** ($\text{Running} \ne \text{Ready}$). A replacement pod does **not** increment `currentHealthy` or replenish `disruptionsAllowed` until its `startupProbe` and `readinessProbe` succeed.
- **Milestone #22 (PodDisruptionBudget):** Establishes the **Planned Disruption Boundary** ($\text{PDB} \ne \text{High Availability}$). It guarantees numerical capacity during voluntary operations.
- **Milestone #23 (Topology Spread Constraints):** Establishes the **Spatial Distribution Boundary**. Even with a PDB, if all 3 replicas land on the same node or Availability Zone, a single failure domain loss causes an outage. Milestone #23 will distribute replicas across failure domains.
