# Planned Disruption Flow & Eviction Architecture

This architecture document visualizes the control flow of **voluntary disruptions** in Kubernetes, detailing how the **Eviction API** interacts with `PodDisruptionBudget` (PDB) during planned maintenance operations (such as `kubectl drain`), and establishing strict boundaries with rollout controllers and involuntary failures.

A foundational reliability principle in Kubernetes is:

$$
\text{PodDisruptionBudget} \ne \text{Application High Availability}
$$

A PodDisruptionBudget defines how much voluntary disruption an already resilient workload can tolerate. It does **not** create application availability by itself.

---

## Control Plane Planned Disruption Flow

The following sequence models the control flow when an operator or automated system (e.g., node auto-drain, Karpenter/Cluster Autoscaler node consolidation, AWS AMI rotation) drains a node:

```mermaid
flowchart TD
    subgraph NORMAL_STATE["1. Steady State Baseline"]
        direction LR
        PodA["Pod A\n(Phase: Running / Ready: True)"]
        PodB["Pod B\n(Phase: Running / Ready: True)"]
        PodC["Pod C\n(Phase: Running / Ready: True)"]
    end

    subgraph DRAIN_TRIGGER["2. Disruption Initiation"]
        Operator["Cluster Operator / Maintenance Automation\n(e.g., AMI upgrade, autoscaler consolidation)"]
        DrainCommand["kubectl drain <node-name>\n(Cordon node + Evict pods)"]
        Operator -->|"Trigger drain"| DrainCommand
    end

    subgraph EVICTION_BOUNDARY["3. Policy-Aware Eviction Boundary"]
        DrainCommand -->|"Calls policy subresource\nPOST /api/v1/namespaces/.../pods/.../eviction"| EvictionAPI["Kubernetes Eviction API"]
        PDBCheck{"PDB Evaluator\nCheck sample-api budget:\ncurrentHealthy >= minAvailable ?"}
        EvictionAPI --> PDBCheck
    end

    subgraph BRANCH_ALLOWED["4A. Disruption Permitted (disruptionsAllowed > 0)"]
        direction TB
        EvictionGranted["Eviction Granted (HTTP 201 Created)"]
        KubeletTerm["Kubelet initiates Pod graceful termination\n(SIGTERM -> preStop -> grace period -> SIGKILL)"]
        EndpointSliceUpdate["EndpointSlice controller removes endpoint\nTraffic redirected to remaining 2 ready pods"]
        ControllerReconcile["Deployment / ReplicaSet controller\nobserves replica deficit (2 < 3)"]
        CreateReplacement["Controller requests new replacement Pod"]
        SchedulerPlacement["kube-scheduler selects eligible worker node\n(Evaluates taints, tolerations, affinity)"]
        ColdStart["New Pod executes startupProbe (/startup)"]
        WarmupPass["readinessProbe succeeds (/ready)\nPod marked Ready: True"]
        BudgetRestored["Disruption budget replenished\n(disruptionsAllowed: 0 -> 1)"]

        EvictionGranted --> KubeletTerm
        KubeletTerm --> EndpointSliceUpdate
        KubeletTerm -.-> ControllerReconcile
        ControllerReconcile --> CreateReplacement
        CreateReplacement --> SchedulerPlacement
        SchedulerPlacement --> ColdStart
        ColdStart --> WarmupPass
        WarmupPass --> BudgetRestored
    end

    subgraph BRANCH_BLOCKED["4B. Disruption Blocked (disruptionsAllowed == 0)"]
        direction TB
        EvictionDenied["Eviction Denied (HTTP 429 Too Many Requests)\nReason: Cannot evict pod as it would violate the pod's disruption budget"]
        DrainWait["kubectl drain pauses / retries\n(Waits for period before re-evaluating)"]
        DrainTimeout["Drain blocks or times out if replacement never becomes ready\nMaintenance halted safely to protect service"]

        EvictionDenied --> DrainWait
        DrainWait --> DrainTimeout
        DrainWait -.->|"Re-evaluates eviction"| EvictionAPI
    end

    %% Routing decision
    NORMAL_STATE -.->|"Target node hosts Pod A"| DrainCommand
    PDBCheck -->|"Yes (3 healthy >= 2 minAvailable)\ndisruptionsAllowed: 1"| EvictionGranted
    PDBCheck -->|"No (2 healthy == 2 minAvailable)\ndisruptionsAllowed: 0"| EvictionDenied

    %% Styling
    classDef normal fill:#0f172a,stroke:#34d399,stroke-width:2px,color:#f8fafc;
    classDef trigger fill:#1e293b,stroke:#38bdf8,stroke-width:2px,color:#f8fafc;
    classDef decision fill:#312e81,stroke:#818cf8,stroke-width:2px,color:#f8fafc;
    classDef allowed fill:#064e3b,stroke:#10b981,stroke-width:2px,color:#ecfdf5;
    classDef blocked fill:#7f1d1d,stroke:#f87171,stroke-width:2px,color:#fef2f2;

    class PodA,PodB,PodC normal;
    class Operator,DrainCommand,EvictionAPI trigger;
    class PDBCheck decision;
    class EvictionGranted,KubeletTerm,EndpointSliceUpdate,ControllerReconcile,CreateReplacement,SchedulerPlacement,ColdStart,WarmupPass,BudgetRestored allowed;
    class EvictionDenied,DrainWait,DrainTimeout blocked;
```

---

## Architectural Boundaries

To operate production Kubernetes clusters reliably, platform engineers must separate four distinct disruption and lifecycle boundaries:

| Mechanism | API Layer | Governed by PDB? | Behavior During Involuntary Crash | Primary Constraint Mechanism |
| :--- | :--- | :--- | :--- | :--- |
| **Voluntary Policy-Aware Eviction** (`kubectl drain`, Karpenter, Cluster Autoscaler) | **Eviction API** (`/v1/pods/{name}/eviction`) | **YES** | N/A (Voluntary operation) | Respects `spec.minAvailable` / `spec.maxUnavailable`. Denied with HTTP 429 if budget is exhausted. |
| **Direct Pod Deletion** (`kubectl delete pod <name>`) | **Core Pod API** (`/api/v1/namespaces/{ns}/pods/{name}`) | **NO** | N/A (Immediate deletion request) | Bypasses Eviction API completely. The pod is deleted regardless of PDB status. |
| **Deployment Rolling Update** (`kubectl set image`, GitOps rollout) | **Workload Controller** (`apps/v1` ReplicaSet controller) | **NO** | N/A (Rollout lifecycle) | Governed exclusively by `spec.strategy.rollingUpdate.maxUnavailable` and `maxSurge`. Deployment controller does not call the Eviction API. |
| **Involuntary Disruption** (Hardware loss, kernel crash, OOM, power failure) | **Physical / Node Infrastructure** | **NO** | Immediate loss of node/pod | PDB cannot prevent physical failure. However, the loss **consumes the disruption budget**, reducing `disruptionsAllowed` to 0. |

---

## Disruption Arithmetic for `sample-api`

In this reference architecture, the workload configuration is:

- `spec.replicas`: **3**
- `spec.minAvailable`: **2**
- `spec.unhealthyPodEvictionPolicy`: **AlwaysAllow**

### Scenario 1: Steady State (All 3 Replicas Ready)
- `desiredHealthy`: 2
- `currentHealthy`: 3
- `disruptionsAllowed`: **1**
- **Result:** Draining a node hosting 1 replica succeeds immediately.

### Scenario 2: During Replacement Warm-Up (Pod A terminating, Pod Replacement starting)
- `desiredHealthy`: 2
- `currentHealthy`: 2 (Pod B and Pod C only)
- `disruptionsAllowed`: **0**
- **Result:** A concurrent attempt to drain the node hosting Pod B is **rejected** with HTTP 429 (`DisruptionAllowed: False`, reason: `InsufficientPods`).

### Scenario 3: Broken Pod (Pod A failing readiness or crashing)
- With `unhealthyPodEvictionPolicy: AlwaysAllow`:
  - Pod A (unhealthy) can be evicted during node drain even if `disruptionsAllowed` is 0.
  - This prevents a broken application replica from permanently halting cluster-wide node upgrades or security patching.

---

## Reliability Progression: Milestone Bridge

The platform reliability story builds across three deliberate milestones:

```text
#21 — Readiness (Workload Probes)
"Can this specific replica serve traffic?"
└── Decouples running state from ready backend routing.

            ↓

#22 — Disruption Budget (PDB)
"How much voluntary disruption can this workload tolerate simultaneously?"
└── Protects serving capacity during planned maintenance via the Eviction API.

            ↓

#23 — Topology Resilience (Topology Spread Constraints)
"Where should replicas run across failure domains (nodes & zones)?"
└── Guarantees pods are not concentrated on a single failure domain.
```

> [!NOTE]
> A PDB ensures **numerical availability** during planned maintenance; it does **not** ensure failure domain distribution. Even with a perfect PDB, if all 3 replicas are scheduled onto the same underlying rack or Availability Zone, a single infrastructure loss will impact the entire service. Milestone #23 addresses topology distribution.
