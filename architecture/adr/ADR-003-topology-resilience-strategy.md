# ADR-003: Workload Topology Resilience Strategy Across Multi-AZ Failure Domains

## Status

Accepted

---

## Context

Production Kubernetes workloads hosted on cloud infrastructure like Amazon EKS operate across multiple physically isolated data center facilities known as **Availability Zones (AZs)**. Each Availability Zone is engineered with independent power, cooling, physical security, and networking to ensure that common infrastructure failures (power grid disruptions, fiber cuts, localized flooding, or facility-level outages) remain isolated to a single zone.

However, Kubernetes clusters do not automatically understand business availability requirements. By default, the `kube-scheduler` places pods onto worker nodes based primarily on node filtering (evaluating resource requests, taints, tolerations, and node selectors) and scoring algorithms (optimizing for resource bin-packing or balanced resource allocation).

Without explicit placement constraints, the scheduler can concentrate multiple replicas—or even all replicas—of a critical workload onto nodes within a single Availability Zone. While all replicas may report `Running` and pass readiness probes under normal conditions, they share a single physical failure domain. If that Availability Zone experiences an outage or performance degradation, all replicas fail simultaneously.

This milestone establishes the third layer in our platform reliability progression:

```text
Milestone #21 (Workload Probes)
Question: "Can this specific replica serve traffic?"
└── Decouples container running state from ready backend routing.

            ↓

Milestone #22 (PodDisruptionBudget)
Question: "Can voluntary disruption proceed safely?"
└── Guarantees numerical serving capacity during planned maintenance.

            ↓

Milestone #23 (Topology Resilience)
Question: "Will another replica survive the failure domain we designed for?"
└── Guarantees spatial distribution across physical failure domains.
```

---

## Problem

A widespread architectural misconception in platform engineering is:

$$
\text{Replica Count} = \text{High Availability}
$$

Setting `spec.replicas: 3` provides **redundancy**, but redundancy without spatial isolation is fragile:

1. **Correlated Placement Risk:** In a 3-replica deployment without topology constraints, the scheduler may place all 3 pods in Zone A (e.g., because Zone A nodes have the lowest current memory utilization). If Zone A suffers an infrastructure failure, 100% of the workload's serving capacity is lost instantly.
2. **Ambiguity in Pod Separation Semantics:** Teams often debate between `podAntiAffinity` and `topologySpreadConstraints` without distinguishing their mathematical semantics. Anti-affinity focuses on binary separation/repulsion ("May/should matching pods share this domain?"), which can cause unintended scheduling deadlocks when scaling workloads beyond the number of zones.
3. **Hard Constraint Failure Boundaries:** Enforcing a hard placement policy (`whenUnsatisfiable: DoNotSchedule`) prevents correlated placement, but introduces a new operational failure mode: if eligible nodes in the required zone are exhausted, tainted, or unlabelled, replacement or scaling pods remain in the `Pending` state. Platform operators must be equipped to distinguish topology constraint enforcement from true scheduler failures or cluster capacity exhaustion.

---

## Decision

We adopt declarative **`topologySpreadConstraints`** within the `spec.template.spec` of the `sample-api` Deployment with the following configuration:

```yaml
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app.kubernetes.io/name: sample-api
```

### Core Architecture Rationale

1. **Explicit Failure Domain (`topologyKey: topology.kubernetes.io/zone`):**
   - Binds the distribution domain to the standard well-known Kubernetes node label `topology.kubernetes.io/zone` (populated automatically by cloud-provider integrations like AWS cloud-controller-manager).
   - Establishes the physical Availability Zone as the explicit boundary of correlated infrastructure risk.

2. **Bounded Distribution (`maxSkew: 1`):**
   - Defines the maximum permitted degree to which matching pods may be unevenly distributed across eligible zones.
   - For a 3-replica deployment distributed across 3 Availability Zones, `maxSkew: 1` enforces an ideal distribution of $1 / 1 / 1$ replicas per zone.
   - In the event of single-replica replacement or dynamic scaling to 4 or 5 replicas, `maxSkew: 1` permits configurations like $2 / 1 / 1$ or $2 / 2 / 1$, preventing any single zone from accumulating an uncontrolled concentration of replicas.
   - *Semantic Precision:* `maxSkew: 1` is **not** an availability percentage, not an HA guarantee, and not a promise that every zone will always host a pod. It is strictly a mathematical bound on distribution imbalance.

3. **Hard Scheduling Enforcement (`whenUnsatisfiable: DoNotSchedule`):**
   - Creates a hard scheduling constraint. If a candidate placement would violate the configured skew boundary across eligible zones, the scheduler **refuses** to place the pod on that candidate node.
   - *Design Invariant:* We intentionally prioritize failure domain isolation over unconstrained placement. Placing a 3rd replica into an already saturated zone creates false confidence in redundancy; leaving the pod `Pending` immediately surfaces the infrastructure capacity deficit to operators and autoscalers.
   - *Semantic Precision:* `DoNotSchedule` does **not** guarantee high availability. It guarantees that the scheduler will not violate the declared topology distribution policy.

4. **Precise Label Selector Matching (`matchLabels: app.kubernetes.io/name: sample-api`):**
   - Reuses the existing Deployment selector (`app.kubernetes.io/name: sample-api`) exactly.
   - Ensures that only matching `sample-api` pods participate in the skew calculation, preventing interference from other workloads in the same namespace or cluster.

---

## Decision Drivers

- **Correlated Failure Mitigation:** Prevent an entire multi-replica service tier from being wiped out by a single physical datacenter or AZ impairment.
- **Bounded Skew vs. Binary Repulsion:** Enable smooth horizontal scaling across available zones without artificial caps imposed by binary anti-affinity rules.
- **Deterministic Scheduling Behavior:** Ensure unambiguous, policy-governed placement that cooperates cleanly with node autoscalers (Karpenter / Cluster Autoscaler).
- **Observable Failure State:** Ensure that capacity deficits in specific failure domains manifest visibly as `Pending` pods with explicit scheduler events, rather than silently concentrating risk.

---

## Alternatives Evaluated

| Strategy | Semantic Question / Mechanism | Technical Evaluation | Verdict |
| :--- | :--- | :--- | :--- |
| **A. No Topology Constraint** | *"Where does this pod fit best based on resources?"* | Maximum scheduling flexibility. Pods pack into whichever nodes have available CPU/memory. High risk of correlated multi-replica failure during AZ outage. | **Rejected** (Unacceptable correlated placement risk) |
| **B. Pod Anti-Affinity (`requiredDuringScheduling`)** | *"May matching pods share this topology domain?"* | Binary repulsion. Enforces at most 1 pod per zone. Works for 3 replicas across 3 zones, but completely deadlocks if replicas exceed zone count (e.g., 4th replica cannot schedule). | **Rejected** (Inflexible; breaks horizontal scaling beyond zone count) |
| **C. Pod Anti-Affinity (`preferredDuringScheduling`)** | *"Should matching pods avoid sharing this topology domain?"* | Soft repulsion. Scheduler prefers spreading, but silently packs pods into a single zone when cluster is under resource pressure, reintroducing correlated risk without warning. | **Rejected** (Unpredictable failure boundary; drifts under load) |
| **D. Topology Spread + `DoNotSchedule`** | *"How evenly should matching pods be distributed across eligible zones?"* | Bounded skew distribution (`maxSkew: 1`). Strict enforcement. Prevents correlated packing, but leaves pods `Pending` if eligible zone capacity is exhausted. | **Accepted** (Explicit fault domain isolation with deterministic failure boundary) |
| **E. Topology Spread + `ScheduleAnyway`** | *"How evenly should matching pods be distributed, if convenient?"* | Soft bounded skew. Scheduler attempts to balance skew, but places pods into unbalanced zones if capacity is tight. Weaker placement guarantees. | **Rejected for baseline** (Silently degrades failure domain protection under stress) |
| **F. Topology Spread with `minDomains`** | *"How many distinct zones must exist before evaluating skew?"* | Advanced constraint requiring a minimum count of eligible topology domains. Useful in clusters with fluctuating or dynamic zone topologies. | **Deferred / Evaluated Only** (Unnecessary complexity for standard static 3-AZ baseline) |

### Detailed Semantic Comparison

#### 1. Topology Spread Constraints vs. Pod Anti-Affinity
We reject the claim that "topology spread is always superior to anti-affinity" or that "anti-affinity is obsolete." Instead, we distinguish them by their core scheduling semantics:
- **Pod Anti-Affinity** answers the question: *"May or should matching pods share this topology domain?"* It is fundamentally a mechanism of **repulsion**. Required anti-affinity (`requiredDuringSchedulingIgnoredDuringExecution`) with `topologyKey: topology.kubernetes.io/zone` permits at most 1 pod per zone. While this works cleanly for a 3-replica deployment in a 3-AZ cluster, scaling the deployment to 4 replicas immediately causes the 4th pod to remain permanently `Pending` because no zone can host more than one replica.
- **Topology Spread Constraints** answer the question: *"How evenly should matching pods be distributed across eligible topology domains?"* It is fundamentally a mechanism of **bounded distribution**. Under `maxSkew: 1`, if the workload scales to 4 replicas across 3 zones, the scheduler permits a distribution of $2 / 1 / 1$, keeping the difference between the most populated zone (2) and least populated zone (1) within the permitted skew ($\le 1$).

#### 2. `whenUnsatisfiable: DoNotSchedule` vs. `ScheduleAnyway`
- `ScheduleAnyway` acts as a soft preference. While it avoids `Pending` pods during localized capacity exhaustion, it silently permits pods to bunch into a single zone during cluster stress. This creates an unmonitored reliability regression where an application appears healthy but has quietly lost its multi-AZ resilience.
- `DoNotSchedule` maintains the architectural boundary: if the cluster cannot provide multi-AZ resilience, the system makes that deficiency explicit by holding the unplaceable pod in `Pending` and emitting `FailedScheduling` events.

#### 3. Architectural Role of `minDomains`
- Introduced in Kubernetes v1.25 (graduated to GA in v1.30), `minDomains` defines the minimum number of eligible topology domains that must exist.
- When `minDomains` is specified, if the number of eligible domains in the cluster is less than `minDomains`, the scheduler treats the missing domains as having 0 pods and factors them into the global skew calculation.
- In our baseline environment, the cluster topology is a fixed 3-AZ deployment (`us-east-1a`, `us-east-1b`, `us-east-1c`). Adding `minDomains` to the baseline manifest introduces cognitive overhead without functional benefit. We document `minDomains` as an architectural option for clusters where worker node groups in certain zones might scale down to zero nodes dynamically.

---

## Consequences

### Positive
- **Failure Domain Resilience:** Replicas are actively distributed across distinct AWS Availability Zones. If any single AZ fails, 2 out of 3 replicas remain active outside the failed domain.
- **Horizontal Scaling Compatibility:** Unlike required pod anti-affinity, `sample-api` can scale horizontally (e.g., to 4, 5, or 6 replicas) while maintaining an even spread across zones without scheduling deadlocks.
- **Deterministic Failure Signals:** In the event of localized zone exhaustion, `DoNotSchedule` prevents silent placement degradation, prompting automated cluster scalers or alerting operators to capacity deficits.

### Negative / Trade-offs
- **Reduced Scheduling Flexibility:** The scheduler can no longer place pods onto any node with available compute. Even if nodes in Zone A have abundant free capacity, the scheduler will refuse to place a replica there if it violates `maxSkew: 1`.
- **Potential for Pending Pods:** If an entire Availability Zone runs out of schedulable capacity or experiences node pool scaling delays, new replacement pods will remain in `Pending` phase.
- **Operational Diagnostic Demands:** Cluster operators must understand how to inspect node topology labels and interpret scheduler `FailedScheduling` events to diagnose why a pod is stuck `Pending`.

---

## Failure Boundaries & Non-Goals

1. **Surviving Replicas Do Not Guarantee Application Availability:**
   - Surviving an AZ failure ($2/3$ replicas remaining) does **not** mean the application continues operating smoothly at 100% capacity.
   - If the remaining 2 replicas are overwhelmed by total traffic, or if downstream resources (e.g., a primary database instance located in the failed AZ) are impaired, user-facing latency and 5xx errors will still occur. Application resilience requires end-to-end multi-AZ design across compute, storage, networking, and data tiers.
2. **Topology Spread Does Not Prevent Infrastructure Outages:**
   - Topology constraints cannot prevent an AWS Availability Zone from experiencing power loss, cooling failure, or network partitioning. The constraint merely ensures the workload does not concentrate all its eggs in that single basket.
3. **Topology Spread Does Not Replace PodDisruptionBudget:**
   - A `PodDisruptionBudget` protects numerical capacity during *voluntary* maintenance (e.g., `kubectl drain`). Topology spread governs spatial placement across *physical failure domains*. Both are required; neither replaces the other.
4. **Topology Spread Does Not Dynamically Provision Infrastructure:**
   - If a zone lacks eligible compute nodes, the scheduler cannot create them. Node autoscalers (such as Karpenter or Cluster Autoscaler) must be configured to provision capacity across all target zones.

---

## Operational Evidence & Diagnostic Strategy

When investigating topology-related scheduling behavior, operators must follow an evidence-based diagnostic hierarchy:

### 1. Inspect Pod Scheduling Events
```bash
kubectl describe pod <pending-pod> -n sample-workloads
```
Look for `FailedScheduling` events specifying topology spread rejection:
```text
Warning  FailedScheduling  ...  0/9 nodes are available: 3 node(s) didn't match PodTopologySpread, 6 node(s) had untolerated taint.
```

### 2. Verify Node Topology Labels
```bash
kubectl get nodes -L topology.kubernetes.io/zone,kubernetes.io/hostname
```
Confirm that all worker nodes have the expected `topology.kubernetes.io/zone` labels populated correctly.

### 3. Audit Active Workload Distribution
```bash
kubectl get pods -n sample-workloads -o wide --selector=app.kubernetes.io/name=sample-api
```
Verify the current count of pods per zone to calculate the active skew.

---

## Safe Recovery Considerations

If a replica remains `Pending` due to topology constraints:
1. **Never immediately weaken the policy** (e.g., switching to `ScheduleAnyway` or removing `topologySpreadConstraints`) without first identifying the underlying failure domain. Weakening policy masks infrastructure problems and reintroduces correlated failure risk.
2. **Restore or expand eligible node capacity** in the under-represented Availability Zone (e.g., scaling up the worker node group or adjusting autoscaler limits).
3. **Resolve node taints or resource exhaustion** on worker nodes in the target zone that prevent the scheduler from utilizing otherwise eligible compute.
4. **Inspect node labels** to ensure no nodes joined the cluster with missing or corrupted `topology.kubernetes.io/zone` labels.

---

## Relationship to Prior Milestones

- **Milestone #21 (Workload Probes):** Ensures that when replicas are placed in an eligible zone, they do not receive user traffic until they pass `/ready` checks.
- **Milestone #22 (PodDisruptionBudget):** Ensures that during planned maintenance (such as draining nodes in one zone for upgrades), the eviction rate is throttled so that at least 2 ready replicas remain active.
- **Milestone #23 (Topology Resilience):** Ensures that the 3 replicas are distributed across separate physical zones, guaranteeing that neither planned maintenance nor sudden hardware failure in a single zone can eliminate all serving capacity.
