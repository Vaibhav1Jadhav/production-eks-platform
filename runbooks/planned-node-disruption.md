# Runbook: Planned Node Disruption & Stuck Node Drain Investigation

## Incident Overview

- **Severity:** Medium to High (Infrastructure Maintenance Blocked / Cluster Upgrade Frozen)
- **Symptom:** A planned worker node drain (`kubectl drain`) blocks, hangs indefinitely, or times out with repeated eviction errors:
  ```text
  error when evicting pod "sample-api-xxxx": Cannot evict pod as it would violate the pod's disruption budget.
  ```
- **Target Audience:** Platform Engineers, SREs, and Kubernetes Cluster Operators.
- **Goal:** Systematically diagnose whether a stuck drain is caused by an over-restrictive PDB, an unready replacement pod, cluster capacity exhaustion, or an application-level readiness failure, and resolve it safely without compromising workload availability.

---

## Outside-In Diagnostic Flow

```text
Node Drain Initiated (kubectl drain / AMI rotation / Autoscaler)
        ↓
Drain Blocks or Times Out
        ↓
Step 1: Isolate Failing Pod & Identify Blocking Disruption Budget
        ↓
Step 2: Inspect PDB Operational State (.status.disruptionsAllowed)
        ↓
Step 3: Analyze Condition: Is disruptionsAllowed == 0?
        ├── YES: Evaluate currentHealthy vs desiredHealthy
        │      │
        │      ├── Sub-Case A: currentHealthy == desiredHealthy (Mathematical Lock)
        │      │      └── Root Cause: minAvailable == total replicas (Over-restrictive PDB)
        │      │
        │      └── Sub-Case B: currentHealthy < desiredHealthy (Capacity Deficit)
        │             ├── Replacement Pod Pending? -> Capacity / Taints / Affinity issue
        │             ├── Replacement Pod NotReady? -> Probe / App startup issue (Return to #21)
        │             └── Pod Crashing / CrashLoop? -> Container runtime failure
        │
        └── NO: disruptionsAllowed > 0 but drain fails
               └── Check for Pods with local storage, DaemonSets, or APF rate limiting
        ↓
Step 4: Execute Safe, Disciplined Remediation
```

---

## Diagnostic Investigation Steps

### Step 1: Identify the Blocking Pod and PDB

When a node drain stalls, capture the exact output to find the blocking pod name and namespace:

```bash
# Check cordoned nodes and active drains
kubectl get nodes -o wide | grep SchedulingDisabled

# Stream recent eviction and scheduling events
kubectl get events -n sample-workloads --sort-by=.lastTimestamp | tail -n 20
```

Identify the `PodDisruptionBudget` guarding the workload:

```bash
kubectl get pdb -n sample-workloads -o wide
```

*Example Output:*
```text
NAME         MIN AVAILABLE   MAX UNAVAILABLE   ALLOWED DISRUPTIONS   AGE   TOTAL
sample-api   2               N/A               0                     12m   3
```

---

### Step 2: Inspect PDB Operational State & Conditions

Extract the detailed status from the Kubernetes API:

```bash
# Verify specification reconciliation (observedGeneration vs metadata.generation)
kubectl get pdb sample-api -n sample-workloads -o jsonpath='{"metadata.generation: "}{.metadata.generation}{", status.observedGeneration: "}{.status.observedGeneration}{"\n"}'

# Check allowed disruptions
kubectl get pdb sample-api -n sample-workloads -o jsonpath='{.status.disruptionsAllowed}{"\n"}'

# Query healthy pod accounting
kubectl get pdb sample-api -n sample-workloads -o jsonpath='{"currentHealthy: "}{.status.currentHealthy}{", desiredHealthy: "}{.status.desiredHealthy}{", expectedPods: "}{.status.expectedPods}{"\n"}'

# Inspect status conditions
kubectl get pdb sample-api -n sample-workloads -o jsonpath='{.status.conditions}' | python -m json.tool
```

#### Interpreting Status & Conditions:
- **Spec Reconciliation (`observedGeneration`):**
  Per the Kubernetes API reference, PDB status fields (such as `disruptionsAllowed` and status conditions) are only confirmed valid for the current specification when `.status.observedGeneration` matches `.metadata.generation`. If `.status.observedGeneration < .metadata.generation`, the disruption controller has not yet reconciled recent changes to `spec.minAvailable` or `spec.selector`.
- **Condition `DisruptionAllowed`:**
  If `type: DisruptionAllowed` has `status: "False"` with `reason: InsufficientPods`, the Eviction API is actively rejecting eviction requests to prevent violating the declared availability floor.
- **Eviction Error Body vs. API Rate Limiting:**
  If the error response states `"Cannot evict pod as it would violate the pod's disruption budget"`, the blockage is confirmed to be PDB-enforced. If it returns HTTP 429 without mentioning disruption budgets, investigate API server FlowSchema / PriorityAndFairness (APF) client-side rate limits.

---

### Step 3: Differentiate Mathematical Lock vs. Capacity Deficit

Examine the workload pods to identify why `disruptionsAllowed` is zero:

```bash
kubectl get pods -n sample-workloads -l app.kubernetes.io/name=sample-api -o wide
```

#### Diagnostic Branch A: Mathematical Lock (`currentHealthy == desiredHealthy`)
- **Evidence:** All 3 pods are in `Running` phase with `1/1 Ready`. However, `minAvailable: 3` and `replicas: 3`.
- **Diagnosis:** The PDB is over-restrictive. It requires 100% availability on an unscaled tier. No single eviction can ever occur without dipping below 3.
- **Remediation:** Proceed to **Remediation Path 1: Recalibrate Budget** or **Remediation Path 2: Scale Workload**.

#### Diagnostic Branch B: Capacity Deficit (`currentHealthy < desiredHealthy`)
- **Evidence:** `desiredHealthy: 2`, but `currentHealthy: 1`. A replacement pod exists but is not ready.
- **Investigate Replacement Pod State:**

1. **Pod is `Pending`:**
   ```bash
   kubectl describe pod <pending-pod-name> -n sample-workloads
   ```
   - Look for `Events`: `FailedScheduling` (e.g., `0/3 nodes are available: 1 node has taints that the pod didn't tolerate, 2 nodes had insufficient memory`).
   - **Remediation:** Cluster is out of schedulable capacity. Proceed to **Remediation Path 3: Cluster Capacity**.

2. **Pod is `Running` but `0/1 Ready`:**
   ```bash
   kubectl describe pod <unready-pod-name> -n sample-workloads
   ```
   - Look for probe failures:
     - `Startup probe failed`: Application cold-start or cache initialization taking longer than allowed threshold.
     - `Readiness probe failed`: HTTP 503 from `/ready` due to database connection saturation or circuit breaker trip.
   - **Cross-Reference:** Follow the **[Milestone #21 Runbook: Pod Running but Users Receive 5xx](pod-running-but-5xx.md)** to isolate the readiness failure. The PDB will not allow further drains until this pod becomes ready.

---

## Safe Remediation Playbook

Follow remediation options in order of safety. Never compromise application availability to speed up infrastructure maintenance.

### Path 1: Recalibrate PDB (For Over-Restrictive Configurations)
If the workload has 3 replicas and can tolerate 1 replica undergoing planned replacement:

```bash
kubectl patch pdb sample-api -n sample-workloads \
  --type='merge' \
  -p '{"spec":{"minAvailable":2}}'
```

Verify that `ALLOWED DISRUPTIONS` increments to `1`:
```bash
kubectl get pdb sample-api -n sample-workloads
```
Resume the node drain.

---

### Path 2: Scale Workload Temporarily (If Serving Capacity Must Be Maintained)
If the application genuinely requires at least 3 active replicas to serve peak load without degradation:

1. Temporarily increase replicas:
   ```bash
   kubectl scale deployment sample-api -n sample-workloads --replicas=4
   ```
2. Wait for the 4th replica to achieve `Ready: True`:
   ```bash
   kubectl rollout status deployment/sample-api -n sample-workloads
   ```
3. Once 4 replicas are ready, `disruptionsAllowed` becomes $4 - 3 = 1$.
4. Execute the node drain safely:
   ```bash
   kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data
   ```
5. After node maintenance is complete and the node is uncordoned, scale replicas back to baseline (3).

---

### Path 3: Add Cluster Capacity (For Pending Replacement Pods)
If replacement pods are stuck in `Pending` due to resource exhaustion:
1. Temporarily uncordon another worker node or scale out the worker node group (e.g., via AWS Auto Scaling Group or Karpenter NodePool).
2. Once new compute joins the cluster, the scheduler will place the Pending replacement pod.
3. Once the replacement passes readiness, the PDB will replenish `disruptionsAllowed`, unblocking the drain.

---

### Prohibited Actions & Dangerous Antipaths

> [!CAUTION]
> **DO NOT DELETE THE PDB TO FORCE A DRAIN.**  
> Deleting the PDB (`kubectl delete pdb`) leaves the workload completely unprotected. Automated draining will immediately evict all remaining pods, dropping serving capacity to zero and creating an outage (see [Unprotected Node Drain](../failure-scenarios/disruptions/unprotected-node-drain.md)).

> [!CAUTION]
> **DO NOT USE `kubectl drain --disable-eviction` AS A ROUTINE FIX.**  
> The `--disable-eviction` flag forces `kubectl drain` to bypass the Eviction API and directly delete pods (`DELETE /api/v1/namespaces/.../pods/...`). This bypasses all graceful disruption policies and must only be used in catastrophic disaster recovery scenarios with senior engineering sign-off.
