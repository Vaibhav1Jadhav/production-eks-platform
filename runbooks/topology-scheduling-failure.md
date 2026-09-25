# Runbook: Topology Scheduling Failure & Pending Pod Investigation

## Incident Overview

- **Severity:** Medium to High (Workload Capacity Deficit / Pod Stuck in Pending / Scaling Blocked)
- **Symptom:** A workload replica for `sample-api` remains stuck in the `Pending` state indefinitely, failing to schedule onto any worker node:
  ```text
  sample-api-7b8f9c6d4b-xk92m   0/1     Pending   0          18m
  ```
- **Target Audience:** Platform Engineers, SREs, and Kubernetes Cluster Operators.
- **Goal:** Systematically isolate whether a `Pending` pod is caused by an unsatisfiable topology spread constraint (`whenUnsatisfiable: DoNotSchedule`), compute resource exhaustion in a target zone, missing/corrupted zone labels, untolerated node taints, or conflicting affinity rules, and restore capacity safely without compromising multi-AZ fault domain isolation.

---

## Outside-In Diagnostic Flow

```text
Workload Replica Stuck in Pending Phase
        ↓
Step 1: Pod Scheduling Evidence
(kubectl describe pod <pending-pod> -n sample-workloads)
Question: "Why did kube-scheduler reject candidate nodes?"
        ↓
Step 2: Topology Domain Audit
(kubectl get nodes -L topology.kubernetes.io/zone,kubernetes.io/hostname)
Question: "What failure domains and zone labels does the scheduler actually see?"
        ↓
Step 3: Current Replica Distribution Audit
(kubectl get pods -n sample-workloads -o wide --selector=app.kubernetes.io/name=sample-api)
Question: "Where are existing replicas running and what is the current zone skew?"
        ↓
Step 4: Classify Failure Domain
        ├── Branch A: Pure Topology Skew (maxSkew: 1 violated on candidate nodes)
        ├── Branch B: Zone Compute Exhaustion (Target zone out of CPU/Memory)
        ├── Branch C: Missing or Malformed Topology Labels (Nodes lack topologyKey)
        ├── Branch D: Untolerated Node Taints (Target zone nodes tainted)
        └── Branch E: Conflicting Node Selectors or Affinities
        ↓
Step 5: Execute Disciplined, Safe Remediation
(Restore/add capacity, fix node labels, resolve taints — DO NOT reflexively weaken policy)
```

---

## Diagnostic Investigation Steps

### Step 1: Capture Pod Scheduling Evidence

Extract the exact scheduler events from the `Pending` pod to understand why nodes were filtered out:

```bash
kubectl describe pod <pending-pod-name> -n sample-workloads
```

Look at the **`Events`** section at the bottom of the output.

#### Illustrative Event Signatures:

*Case 1: Mixed Topology Spread and Resource Exhaustion*
```text
Events:
  Type     Reason            Age   From               Message
  ----     ------            ----  ----               -------
  Warning  FailedScheduling  35s   default-scheduler  0/9 nodes are available: 6 node(s) didn't match PodTopologySpread, 3 node(s) had insufficient cpu.
```
> **Interpretation:** 6 nodes in Zones A & B were rejected by `PodTopologySpread` because placing a pod there would violate `maxSkew: 1`. The remaining 3 nodes in Zone C passed topology spread, but failed `NodeResourcesFit` due to insufficient CPU.

*Case 2: Pure Topology Skew Violation*
```text
Events:
  Type     Reason            Age   From               Message
  ----     ------            ----  ----               -------
  Warning  FailedScheduling  20s   default-scheduler  0/6 nodes are available: 6 node(s) didn't match PodTopologySpread.
```
> **Interpretation:** Every available node in the cluster would cause distribution skew to exceed `maxSkew: 1` (e.g., all available nodes belong to zones that already hold the maximum allowed pods).

---

### Step 2: Audit Cluster Topology Domains & Labels

Verify that the Kubernetes control plane accurately detects all Availability Zones and that nodes are properly labelled with `topology.kubernetes.io/zone`:

```bash
kubectl get nodes -L topology.kubernetes.io/zone,kubernetes.io/hostname
```

#### Illustrative Output:
```text
NAME                          STATUS   ROLES    AGE   VERSION   ZONE         HOSTNAME
ip-10-0-1-101.ec2.internal    Ready    <none>   12d   v1.31.0   us-east-1a   ip-10-0-1-101.ec2.internal
ip-10-0-1-102.ec2.internal    Ready    <none>   12d   v1.31.0   us-east-1a   ip-10-0-1-102.ec2.internal
ip-10-0-2-201.ec2.internal    Ready    <none>   12d   v1.31.0   us-east-1b   ip-10-0-2-201.ec2.internal
ip-10-0-2-202.ec2.internal    Ready    <none>   12d   v1.31.0   us-east-1b   ip-10-0-2-202.ec2.internal
ip-10-0-3-301.ec2.internal    Ready    <none>   12d   v1.31.0   <none>       ip-10-0-3-301.ec2.internal
```

#### Key Diagnostic Checks:
1. **Are any nodes missing the `topology.kubernetes.io/zone` label?**
   Notice `ip-10-0-3-301.ec2.internal` above has `<none>` for `ZONE`. If nodes in an AZ lack the topology label, the scheduler cannot count them as part of that topology domain.
2. **Are all expected Availability Zones present?**
   If an entire zone has 0 worker nodes (e.g., all nodes in `us-east-1c` were cordoned, terminated, or failed to join), the scheduler cannot satisfy multi-zone spread under `DoNotSchedule`.

---

### Step 3: Audit Current Replica Placement & Calculate Active Skew

Query all existing pods of the workload using the **actual workload selector**:

```bash
kubectl get pods -n sample-workloads -o wide --selector=app.kubernetes.io/name=sample-api
```

#### Illustrative Output:
```text
NAME                          READY   STATUS    NODE                         ZONE
sample-api-7b8f9c6d4b-2vkm8   1/1     Running   ip-10-0-1-101.ec2.internal   us-east-1a
sample-api-7b8f9c6d4b-9qx7p   1/1     Running   ip-10-0-2-201.ec2.internal   us-east-1b
sample-api-7b8f9c6d4b-xk92m   0/1     Pending   <none>                       <none>
```

#### Skew Accounting:
- Current count in `us-east-1a`: 1
- Current count in `us-east-1b`: 1
- Current count in `us-east-1c`: 0
- Global minimum count across all domains: 0
- **Skew Evaluation:** If the pending pod were placed in `us-east-1a`, that zone would have 2 pods while `us-east-1c` has 0.
  $$\text{Proposed Skew} = 2 - 0 = 2$$
  Since $2 > \text{maxSkew}(1)$, placing this pod in `us-east-1a` or `us-east-1b` is strictly forbidden by `DoNotSchedule`. The pod **must** land in `us-east-1c`.

---

### Step 4: Classify the Failure Domain

Correlate the evidence from Steps 1, 2, and 3:

| Observation | Classification | Underlying Problem |
| :--- | :--- | :--- |
| Scheduler reports `insufficient cpu/memory` on nodes in the under-represented zone | **Zone Capacity Exhaustion** | Worker nodes in the target AZ exist, but lack free allocatable resources. |
| Target zone has 0 worker nodes or nodes are in `NotReady` / `SchedulingDisabled` state | **Zone Infrastructure Deficit** | Node group has scaled to zero in that zone, or instances experienced hypervisor failure. |
| Nodes in the target zone lack `topology.kubernetes.io/zone` label | **Topology Label Configuration Defect** | Cloud-controller-manager or custom bootstrap script omitted standard topology labels. |
| Target zone nodes have untolerated taints (e.g., `dedicated=batch:NoSchedule`) | **Taint / Toleration Conflict** | Nodes in target zone are reserved for other workloads. |
| Pod requires specific `nodeSelector` or affinity not present on target zone nodes | **Affinity / Selector Conflict** | Placement rules are mutually contradictory. |

---

## Safe Remediation Playbook

Follow remediation options in order of safety. Never compromise application fault isolation to resolve a scheduling hold.

### Path 1: Scale Compute Capacity in the Constrained Zone
If the target zone is compute-constrained:
1. Trigger a scale-out of the worker node group in that specific Availability Zone (e.g., via AWS Auto Scaling Group, Karpenter NodePool, or Cluster Autoscaler):
   ```bash
   # Illustrative AWS CLI command to scale specific AZ ASG
   aws autoscaling set-desired-capacity \
     --auto-scaling-group-name eks-nodegroup-us-east-1c \
     --desired-capacity <new-capacity>
   ```
2. Verify the new node joins the cluster and reports `Ready`:
   ```bash
   kubectl get nodes -l topology.kubernetes.io/zone=us-east-1c
   ```
3. Once schedulable compute appears in the target zone, the scheduler will immediately bind the `Pending` pod without operator intervention.

---

### Path 2: Remediate Missing or Corrupted Node Labels
If worker nodes in the target zone are missing topology labels due to a provisioning defect:
1. Inspect node labels:
   ```bash
   kubectl get node <target-node-name> --show-labels
   ```
2. Apply the canonical topology label if safely permitted by operational policy:
   ```bash
   kubectl label node <target-node-name> topology.kubernetes.io/zone=us-east-1c
   ```
3. Check pod status:
   ```bash
   kubectl get pod -n sample-workloads --selector=app.kubernetes.io/name=sample-api
   ```

---

### Path 3: Resolve Untolerated Node Taints
If target zone nodes are tainted:
1. Inspect node taints:
   ```bash
   kubectl describe node <target-node-name> | grep Taints
   ```
2. If the taint was placed accidentally or during an earlier maintenance window that completed:
   ```bash
   kubectl taint nodes <target-node-name> <taint-key>-
   ```

---

### Path 4: Re-evaluate Placement Policy (Architectural Review Only)
If the cluster architecture fundamentally cannot provide 3 operational zones (e.g., temporary regional AWS service degradation, or running in a 2-AZ non-production environment):
1. **Option A: Increase `maxSkew` (if architecture permits):**
   If business requirements permit an imbalance of 2 pods (e.g., $2/1/0$), patch `maxSkew: 2`.
2. **Option B: Soften to `whenUnsatisfiable: ScheduleAnyway`:**
   If the service prioritizes immediate replica availability over strict failure domain isolation during disaster recovery, update the constraint to `ScheduleAnyway`.

> [!WARNING]
> Adjusting `maxSkew` or softening to `ScheduleAnyway` permanently alters the failure boundary. It must be approved as an architectural change, not executed as an unmonitored hotfix.

---

## Prohibited Actions & Dangerous Antipaths

> [!CAUTION]
> **DO NOT DELETE `topologySpreadConstraints` TO CLEAR A PENDING POD.**
> Stripping topology constraints allows the scheduler to dump all replicas into an already crowded zone. While this clears the `Pending` alarm, it creates catastrophic correlated placement risk. If that zone experiences an outage, 100% of workload replicas will be lost simultaneously (see [Correlated Zone Placement](../failure-scenarios/topology/correlated-zone-placement.md)).

> [!CAUTION]
> **DO NOT ASSUME A PENDING POD IS A SCHEDULER BUG.**
> The scheduler is enforcing the exact policy you defined. Investigate the failure domain evidence before taking corrective action.
