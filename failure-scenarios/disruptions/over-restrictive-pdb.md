# Failure Scenario: Over-Restrictive PDB (Maintenance Deadlock)

## Failure Chain

```text
Operator or automated pipeline initiates node drain (kubectl drain)
        ↓
Eviction API invoked for workload pod on target node
        ↓
Eviction API evaluates PodDisruptionBudget (sample-api)
        ↓
Disruption budget is exhausted or over-constrained (disruptionsAllowed = 0)
        ↓
Eviction API denies eviction request with HTTP 429 Too Many Requests
        ↓
kubectl drain pauses and repeatedly retries eviction
        ↓
Drain operation hangs indefinitely or times out
        ↓
Node OS patching, security updates, or AMI rollouts are completely blocked
```

---

## 1. Trigger

A routine infrastructure maintenance task is initiated:
- An SRE or cluster upgrade automation begins draining a worker node to apply critical Linux kernel security patches.
- The command executed is:
  ```bash
  kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data
  ```

---

## 2. Symptom

The node drain process hangs indefinitely, logging repeated eviction failures:

```text
node/ip-10-0-1-42.ec2.internal cordoned
evicting pod sample-workloads/sample-api-7b8f9c6d4b-2vkm8
error when evicting pod "sample-api-7b8f9c6d4b-2vkm8" (will retry after 5s): Cannot evict pod as it would violate the pod's disruption budget.
evicting pod sample-workloads/sample-api-7b8f9c6d4b-2vkm8
error when evicting pod "sample-api-7b8f9c6d4b-2vkm8" (will retry after 5s): Cannot evict pod as it would violate the pod's disruption budget.
```

Automated cluster upgrade tools (e.g., AWS EKS managed node group upgrades, Terraform, or Karpenter) time out, fail the deployment pipeline, and leave worker nodes cordoned in `SchedulingDisabled` status.

---

## 3. Failure Domain

**Boundary:** Boundary between **Application Availability Constraints** and **Cluster Infrastructure Lifecycle**.

An overly conservative or mathematically locked disruption budget protects the workload so aggressively that it makes the underlying cluster infrastructure completely unmaintainable.

---

## 4. Evidence & Diagnostic Investigation

### 1. PDB Status Shows Zero Disruptions Allowed
Querying the PDB reveals that no disruptions are permitted:

```bash
kubectl get pdb sample-api -n sample-workloads -o wide
```

*Example Output:*
```text
NAME         MIN AVAILABLE   MAX UNAVAILABLE   ALLOWED DISRUPTIONS   AGE   TOTAL
sample-api   3               N/A               0                     14d   3
```

Notice:
- `MIN AVAILABLE`: 3
- `TOTAL`: 3
- `ALLOWED DISRUPTIONS`: **0**

$$\text{Current Healthy (3)} - \text{Desired Min (3)} = \mathbf{0}$$

### 2. Eviction API Response & Conditions
Inspecting the PDB status conditions reveals explicit control plane evidence:

```bash
kubectl get pdb sample-api -n sample-workloads \
  -o jsonpath='{.status.conditions}' | python -m json.tool
```

*Example Output:*
```json
[
    {
        "lastTransitionTime": "2026-09-04T08:14:22Z",
        "message": "The number of pods is insufficient to allow any disruptions.",
        "reason": "InsufficientPods",
        "status": "False",
        "type": "DisruptionAllowed"
    }
]
```

### 3. Critical Diagnostic Precision: HTTP 429 Semantics
> [!IMPORTANT]
> **HTTP 429 does NOT always mean a PDB violation.**  
> The Kubernetes API server returns HTTP 429 (`Too Many Requests`) for multiple distinct conditions:
> 1. **PDB Eviction Rejection:** Returned specifically by the Eviction subresource (`/eviction`) when `disruptionsAllowed == 0`. The error body contains: `"Cannot evict pod as it would violate the pod's disruption budget."`
> 2. **API Server Rate Limiting:** Returned by FlowSchema / PriorityAndFairness (APF) when client requests exceed token bucket throughput limits.
> 
> Operators must inspect the error message body and verify PDB `.status.conditions` (`reason: InsufficientPods`) rather than assuming every HTTP 429 is a PDB constraint.

---

## 5. Root Cause

The disruption budget was configured with `minAvailable: 3` on a Deployment with only `replicas: 3` (or equivalently, `maxUnavailable: 0` or `minAvailable: 100%`).

Under this arithmetic:
- Evicting even one pod would reduce healthy replicas to 2.
- Because $2 < 3$, the Eviction API **must reject the eviction** to uphold the declared contract.
- Because no pod can be evicted, no replacement pod can be triggered by the eviction, creating a permanent mathematical deadlock.

---

## 6. Safe Recovery

Platform engineers should follow a disciplined remediation sequence:

### Option A: Calibrate the PDB Budget (Recommended)
Adjust the PDB to tolerate at least one voluntary eviction:
```bash
kubectl patch pdb sample-api -n sample-workloads \
  --type='merge' \
  -p '{"spec":{"minAvailable":2}}'
```

Once applied, `ALLOWED DISRUPTIONS` immediately increments to `1`, allowing `kubectl drain` to proceed safely.

### Option B: Scale the Workload Horizontally
If the application genuinely requires at least 3 running replicas at all times to handle peak production traffic, scale the Deployment capacity before draining:
```bash
kubectl scale deployment sample-api -n sample-workloads --replicas=4
```
Once the 4th replica is `Ready`, `ALLOWED DISRUPTIONS` becomes:
$$4 - 3 = 1$$
The drain can now proceed without reducing serving capacity below the required 3-replica threshold.

### Dangerous Antipaths to Avoid:
- **Do NOT delete the PDB indiscriminately:** Deleting the PDB removes all protection, risking the [Unprotected Node Drain](unprotected-node-drain.md) failure mode.
- **Do NOT use `kubectl drain --disable-eviction` without approval:** Bypassing eviction uses direct pod deletion, ignoring all workload lifecycle protections.

---

## 7. Prevention & New Failure Boundary

- **Prevention:** Standardize workload sizing patterns. For small workloads (e.g., 2–3 replicas), `minAvailable` should never equal total replicas unless automated horizontal scaling precedes maintenance windows.
- **CI/CD Linting:** Implement static manifest linting in `validate.sh` to ensure `deployment.spec.replicas > pdb.spec.minAvailable`.
- **New Failure Boundary:** Setting `minAvailable: 2` permits node drain, but requires that replacement pods become ready promptly. If a replacement pod fails its `readinessProbe` (as analyzed in Milestone #21), subsequent drains will pause until application health is restored.
