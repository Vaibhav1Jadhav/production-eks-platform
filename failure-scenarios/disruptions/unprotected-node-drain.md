# Failure Scenario: Unprotected Node Drain (Insufficient Protection)

## Failure Chain

```text
Automated node maintenance triggered (AMI update / Karpenter consolidation)
        ↓
Multiple worker nodes drained concurrently or in rapid succession
        ↓
Eviction API invoked for workload pods
        ↓
No PodDisruptionBudget configured (or selector misconfigured)
        ↓
Eviction API grants all eviction requests immediately
        ↓
Multiple replicas enter Terminating state simultaneously
        ↓
Active serving capacity collapses below baseline demand
        ↓
Surviving replicas become saturated / queue-bound
        ↓
Users experience elevated latency, degraded throughput, or HTTP 503/504 errors
```

---

## 1. Trigger

A routine cluster maintenance event occurs:
- An automated AWS EKS Managed Node Group AMI update initiates a rolling replace of worker nodes.
- A cluster autoscaler (Karpenter / Cluster Autoscaler) consolidates underutilized EC2 instances to reduce infrastructure costs.
- An SRE drains multiple worker nodes simultaneously to decommission hardware or apply urgent kernel security patches.

---

## 2. Symptom

During the maintenance window, end users experience:
- Elevated p95 and p99 response latencies.
- Intermittent HTTP 502 Bad Gateway or HTTP 503 Service Unavailable errors.
- Dropped TCP connections or connection timeouts during inflight requests.

Simultaneously, the monitoring dashboard shows a steep drop in active ready endpoints for the service, despite the Deployment controller actively attempting to spin up replacements.

---

## 3. Failure Domain

**Boundary:** Boundary between **Infrastructure Maintenance Automation** and **Application Capacity Guarantees**.

Without a `PodDisruptionBudget`, the Kubernetes control plane treats all pods as immediately dispensable during voluntary eviction. The Eviction API has no policy constraint to throttle node drains to the speed of application bootstrapping and probe validation.

---

## 4. Evidence

### 1. PDB Query Shows Absence of Protection
Querying the namespace reveals no disruption budget protects the workload:

```bash
kubectl get pdb -n sample-workloads
```

*Output:*
```text
No resources found in sample-workloads namespace.
```

### 2. Simultaneous Pod Terminations
`kubectl get pods` displays multiple replicas entering the `Terminating` state within seconds of each other:

```bash
kubectl get pods -n sample-workloads -o wide
```

*Example Output:*
```text
NAME                          READY   STATUS        AGE   IP            NODE
sample-api-7b8f9c6d4b-2vkm8   1/1     Terminating   12m   10.244.1.5    node-worker-1
sample-api-7b8f9c6d4b-9qx7p   1/1     Terminating   12m   10.244.2.8    node-worker-2
sample-api-7b8f9c6d4b-lm4tz   1/1     Running       12m   10.244.3.4    node-worker-3
sample-api-7b8f9c6d4b-w8jkl   0/1     Pending       2s    <none>        <none>
```

Two out of three replicas ($67\%$ of total serving capacity) are terminating simultaneously.

### 3. EndpointSlice Depletion
Inspecting the active `EndpointSlice` demonstrates that only one backend remains marked `ready: true`:

```bash
kubectl get endpointslice -l kubernetes.io/service-name=sample-api -n sample-workloads -o yaml
```

*Excerpt:*
```yaml
endpoints:
  - addresses:
      - 10.244.3.4
    conditions:
      ready: true
      serving: true
      terminating: false
    targetRef:
      name: sample-api-7b8f9c6d4b-lm4tz
```

The sole remaining pod is overwhelmed by full production traffic, resulting in thread starvation and request queue timeouts.

---

## 5. Root Cause

1. **Missing Disruption Policy:** The workload lacked a `PodDisruptionBudget` matching its selector (`app.kubernetes.io/name: sample-api`).
2. **Unconstrained Eviction API:** The Eviction API granted every eviction request immediately upon receipt, assuming the workload had sufficient elasticity to absorb concurrent terminations.
3. **Replacement Warm-up Lag:** While the Deployment controller promptly created replacement pods, the new pods required time to pull images, execute `/startup` bootstrapping, and pass `/ready` probes. Because the old pods were terminated before replacements were ready, capacity dropped below the minimum operational threshold.

---

## 6. Recovery

### Immediate Operational Relief
1. Pause automated node draining or cancel pending AMI rotations:
   ```bash
   # Uncordon nodes to allow scheduling if capacity is constrained
   kubectl uncordon <node-name>
   ```
2. Temporarily scale up Deployment replicas to compensate for lost pods:
   ```bash
   kubectl scale deployment sample-api -n sample-workloads --replicas=5
   ```

### Long-Term Architectural Fix
1. Define and apply a calibrated `PodDisruptionBudget` declaring `minAvailable: 2` (or `maxUnavailable: 1`):
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
2. Integrate PDB validation into the CI/CD deployment pipeline or admission control (e.g., OPA Gatekeeper / Kyverno) to prevent deploying production workloads without an accompanying disruption budget.

---

## 7. Prevention & New Failure Boundary

- **Prevention:** A calibrated PDB guarantees that the Eviction API will only allow 1 voluntary eviction at a time. The Eviction API will reject subsequent drain attempts until the replacement pod becomes `Ready`.
- **New Failure Boundary:** Establishing a PDB introduces a new operational constraint: **over-restrictive budgets can block node maintenance**. If `minAvailable` is set too high or replacement pods fail to initialize, cluster upgrades will hang. This trade-off is examined in [Over-Restrictive PDB](over-restrictive-pdb.md).
