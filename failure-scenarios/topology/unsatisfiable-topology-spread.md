# Failure Scenario: Unsatisfiable Topology Spread & The Pending Pod Boundary

## Failure Chain

```text
Deployment configured with topologySpreadConstraints:
├── topologyKey: topology.kubernetes.io/zone
├── maxSkew: 1
└── whenUnsatisfiable: DoNotSchedule
        ↓
Scaling event occurs (e.g., replicas 3 -> 4) OR a replica terminates and requires replacement
        ↓
kube-scheduler evaluates all candidate nodes across all Availability Zones
        ↓
Candidate nodes in Zone A or Zone B would violate maxSkew: 1 (skew would exceed 1)
        ↓
Zone C is the only eligible topology domain that satisfies the skew constraint
        ↓
However, candidate nodes in Zone C fail one or more scheduler filter plugins:
├── Insufficient CPU or Memory requests (compute exhaustion in Zone C), OR
├── Untolerated taints (e.g., dedicated node pool or draining node), OR
├── Conflicting nodeSelectors or node affinity rules, OR
├── Conflicting pod affinity or anti-affinity rules, OR
└── Missing or corrupted topology.kubernetes.io/zone label on Zone C nodes
        ↓
kube-scheduler cannot find ANY eligible node that satisfies BOTH topology skew AND filter plugins
        ↓
Hard constraint enforcement (DoNotSchedule) takes effect
        ↓
Replacement / scaling Pod remains stuck in the PENDING phase
        ↓
FailedScheduling event emitted; operator investigation required
```

---

## 1. Trigger & Failure Context

Introducing `whenUnsatisfiable: DoNotSchedule` deliberately establishes a hard scheduling boundary:

$$
\text{Failure Domain Isolation} > \text{Unconstrained Placement}
$$

When a workload scales up or when a pod is evicted and requires replacement, the scheduler calculates the distribution skew across all eligible topology domains matching `topologyKey: topology.kubernetes.io/zone`.

If placing the pod into Zone A or Zone B would push the skew above `maxSkew: 1`, the scheduler **must** place the pod into the under-represented zone (e.g., Zone C). However, if no node in Zone C can accept the pod, the pod cannot be scheduled anywhere.

The pod enters and remains in the **`Pending`** phase.

> [!IMPORTANT]
> **Evidence Status: DESIGNED / ILLUSTRATIVE**
> A pod stuck in `Pending` is **not** automatically a "scheduler bug" or "scheduler failure." The scheduler may be operating with 100% correctness, faithfully enforcing the policy that placing the pod in an over-represented zone violates the architectural constraint against correlated placement risk.

---

## 2. Symptom

- A replacement or newly scaled pod remains in the `Pending` state indefinitely:
  ```text
  NAME                          READY   STATUS    RESTARTS   AGE
  sample-api-7b8f9c6d4b-xk92m   0/1     Pending   0          18m
  ```
- Deployment available replicas fails to reach desired replicas (`2/3` or `3/4` ready).
- If this occurs following a voluntary disruption or node drain, the `PodDisruptionBudget` may drop `disruptionsAllowed` to `0`, causing subsequent node drains to block (cross-reference [Runbook: Planned Node Disruption](../../runbooks/planned-node-disruption.md)).

---

## 3. Dissecting Root Causes: Distinguishing the Failure Domain

> [!CAUTION]
> **Never write: "Pending Pod = topologySpreadConstraints problem."**
> Evidence must determine the actual failure domain. A `Pending` pod indicates that *no candidate node satisfied all scheduling requirements simultaneously*. The root cause may be compute capacity, taints, label mismatches, or conflicting affinity rules rather than the skew configuration itself.

Platform engineers must systematically classify the root cause using scheduler evidence:

| Root Cause Category | Diagnostic Signature in `kubectl describe pod` | Underlying Failure Mechanism |
| :--- | :--- | :--- |
| **A. Pure Topology Skew Constraint** | `0/9 nodes are available: 6 node(s) didn't match PodTopologySpread, 3 node(s) had insufficient memory.` | Nodes in Zone A & B have free resources, but are filtered out by `PodTopologySpread` to preserve `maxSkew: 1`. Nodes in Zone C cannot accept the pod due to resource constraints. |
| **B. Missing Topology Zone Labels** | `0/6 nodes are available: 6 node(s) didn't match PodTopologySpread.` (Nodes lack `topology.kubernetes.io/zone`) | New worker nodes joined the cluster without cloud-provider topology labels (e.g., manual node bootstrap or CCM failure). The scheduler cannot evaluate zones. |
| **C. Inconsistent Topology Labels** | Skew calculations produce unexpected global minimums due to misspelled or legacy zone labels (e.g., mixing `failure-domain.beta.kubernetes.io/zone` with `topology.kubernetes.io/zone`). | The scheduler treats nodes with different label keys as belonging to completely separate domains. |
| **D. Zone Compute Exhaustion** | `0/9 nodes are available: 3 node(s) had insufficient cpu, 3 node(s) had insufficient memory, 3 node(s) didn't match PodTopologySpread.` | The target zone's worker nodes are completely saturated. The node autoscaler (Karpenter/CAS) has reached maximum pool size or failed to launch an instance. |
| **E. Untolerated Node Taints** | `0/9 nodes are available: 3 node(s) had untolerated taint, 6 node(s) didn't match PodTopologySpread.` | Nodes in the required zone possess taints (e.g., `node.kubernetes.io/unreachable`, `CriticalAddonsOnly`, or dedicated workload taints) that the pod does not tolerate. |
| **F. Conflicting Node Selectors / Affinity** | `0/9 nodes are available: 3 node(s) didn't match PodTopologySpread, 6 node(s) didn't match Pod's node affinity/selector.` | The pod has a `nodeSelector` (e.g., `workload-tier: api`) that does not exist on nodes located in the required zone. |
| **G. Conflicting Pod Anti-Affinity** | `0/9 nodes are available: 3 node(s) didn't match PodTopologySpread, 3 node(s) didn't match pod anti-affinity rules.` | An uncoordinated `podAntiAffinity` rule conflicts with the topology spread constraint, rejecting placements that the spread plugin would otherwise allow. |

---

## 4. Operational Evidence & Inspection

*(The following command outputs illustrate how to capture scheduler evidence and are explicitly labeled as **ILLUSTRATIVE**.)*

### Step 1: Capture Scheduler Rejection Events
```bash
kubectl describe pod <pending-pod> -n sample-workloads
```

*Illustrative Output:*
```text
Events:
  Type     Reason            Age   From               Message
  ----     ------            ----  ----               -------
  Warning  FailedScheduling  42s   default-scheduler  0/9 nodes are available: 6 node(s) didn't match PodTopologySpread, 3 node(s) had insufficient cpu. preemption: 0/9 nodes are available: 9 Preemption is not helpful for scheduling.
```

**Diagnostic Interpretation:**
- 6 nodes (situated in Zones A and B) were rejected because placing this replica there would violate `maxSkew: 1`.
- 3 nodes (situated in Zone C) were valid according to topology spread, but were rejected by the `NodeResourcesFit` plugin due to insufficient CPU capacity.
- **True Failure Domain:** Compute resource exhaustion in Zone C, strictly bound by the topology policy.

### Step 2: Audit Node Zone Distribution & Resource Pressure
```bash
kubectl get nodes -L topology.kubernetes.io/zone -o wide
```

*Illustrative Output:*
```text
NAME                          STATUS   ZONE         INTERNAL-IP   ALLOCATABLE-CPU
ip-10-0-1-101.ec2.internal    Ready    us-east-1a   10.0.1.101    3920m
ip-10-0-1-102.ec2.internal    Ready    us-east-1a   10.0.1.102    3920m
ip-10-0-2-201.ec2.internal    Ready    us-east-1b   10.0.2.201    3920m
ip-10-0-2-202.ec2.internal    Ready    us-east-1b   10.0.2.202    3920m
ip-10-0-3-301.ec2.internal    Ready    us-east-1c   10.0.3.301    120m (Constrained)
```

**Diagnostic Interpretation:** `us-east-1c` has only one worker node, and its allocatable CPU is down to 120m, which is insufficient to satisfy the pod's 50m request plus overhead.

---

## 5. Recovery Path

Remediation must address the underlying constraint, never weaken the policy blindly:

### 1. Safe Remediation (Add Zone Capacity)
- Scale the worker node pool in the constrained zone (e.g., `us-east-1c`) via Karpenter `NodePool` or AWS Auto Scaling Group:
  ```bash
  aws autoscaling set-desired-capacity --auto-scaling-group-name eks-nodes-us-east-1c --desired-capacity 2
  ```
- Once the new node joins the cluster and reports `Ready` with `topology.kubernetes.io/zone=us-east-1c`, the scheduler automatically binds the `Pending` pod.

### 2. Antipattern: Weakening Policy Reflexively
> [!CAUTION]
> **Do NOT change `DoNotSchedule` to `ScheduleAnyway` as a quick fix.**
> Switching to `ScheduleAnyway` allows the scheduler to pack the pod into Zone A or Zone B. While this clears the `Pending` state, it destroys failure domain isolation, leaving the workload vulnerable to a single-zone catastrophic outage. Fix the capacity deficit in the target zone instead.
