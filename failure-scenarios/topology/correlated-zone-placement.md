# Failure Scenario: Correlated Zone Placement Risk

## Failure Chain

```text
Deployment configured with 3 replicas, but WITHOUT explicit topologySpreadConstraints
        ↓
kube-scheduler evaluates candidate nodes using standard resource filtering and scoring
        ↓
Illustrative Placement: Nodes in a single Availability Zone (e.g., Zone A) have optimal resource fit
        ↓
All 3 replicas are placed on nodes belonging to Zone A
        ↓
All 3 pods pass startup and readiness probes (Status: Running, Ready: True)
        ↓
False sense of security: Deployment reports 3/3 ready replicas
        ↓
Physical / facility impairment occurs in Zone A (power loss, network partition, cooling failure)
        ↓
All nodes in Zone A become NotReady / Unreachable simultaneously
        ↓
All 3 workload replicas become unavailable simultaneously (0/3 ready capacity)
        ↓
Complete service outage: Client requests fail with HTTP 503 / connection refused
```

---

## 1. Symptom & Architecture Risk

In this scenario, a workload is declared with `spec.replicas: 3`. Under normal operating conditions, all three replicas are in `Running` phase, passing readiness checks, and serving traffic through their Kubernetes Service.

However, because the workload specification lacks explicit topology spreading requirements, the scheduler is free to place pods according to its default scoring algorithms. In an illustrative placement, all three replicas are scheduled onto worker nodes situated within a single AWS Availability Zone (e.g., `us-east-1a`).

When an unexpected physical infrastructure impairment affects that Availability Zone:
- All three replicas become unreachable or terminate simultaneously.
- Active serving endpoints drop from 3 to 0.
- External clients encounter connection timeouts and HTTP 503 errors.

The architectural failure is not *"replicas did not exist."*
The failure is: **the replicas shared a single correlated failure domain.**

> [!IMPORTANT]
> **Evidence Status: DESIGNED / ILLUSTRATIVE**
> This placement represents an illustrative possible outcome. We do **not** claim that Kubernetes always places all replicas in a single zone when constraints are absent; rather, without an explicit topology-spreading requirement, replica count alone does **not** guarantee distribution across the failure domains the application architecture relies upon.

---

## 2. Possible Placement (Illustrative Example)

Consider an EKS cluster spanning three Availability Zones (`us-east-1a`, `us-east-1b`, `us-east-1c`).

Without `topologySpreadConstraints`, the scheduler evaluates node resources independently. If nodes in `us-east-1a` were recently added or have significantly lower memory utilization, the scheduler's scoring plugin may schedule all replicas into that zone:

| Replica | Assigned Node (Illustrative) | Node Zone Label (Illustrative) | Status |
| :--- | :--- | :--- | :--- |
| `sample-api-7b8f9c6d4b-2vkm8` | `ip-10-0-1-101.ec2.internal` | `us-east-1a` | Ready |
| `sample-api-7b8f9c6d4b-9qx7p` | `ip-10-0-1-102.ec2.internal` | `us-east-1a` | Ready |
| `sample-api-7b8f9c6d4b-lm4tz` | `ip-10-0-1-103.ec2.internal` | `us-east-1a` | Ready |

All three pods run on separate EC2 instances, protecting against single-node crashes. But they share the exact same physical facility, power distribution units, and network switches.

---

## 3. Failure Domain Analysis

- **Target Failure Domain:** Physical Availability Zone (`topology.kubernetes.io/zone`).
- **Correlated Risk:** When an entire Availability Zone experiences a major failure (e.g., loss of external utility power or major transit provider fiber severance), every node in that zone loses connectivity with the Kubernetes control plane.
- **Control Plane Reaction Time:** The Kubernetes control plane does not instantly re-create pods. By default, `node-controller` waits `node-monitor-grace-period` (default 40s) before marking nodes `NotReady`, and then eviction taint thresholds apply before replacement pods can be scheduled onto surviving zones. During this multi-minute convergence window, available capacity is 0%.

---

## 4. Operational Evidence & Inspection

*(The following command outputs illustrate how this failure mode manifests and are explicitly labeled as **ILLUSTRATIVE**.)*

### 1. Inspect Pod Placement Across Nodes
```bash
kubectl get pods -n sample-workloads -o wide --selector=app.kubernetes.io/name=sample-api
```

*Illustrative Output:*
```text
NAME                          READY   STATUS    RESTARTS   AGE   IP           NODE
sample-api-7b8f9c6d4b-2vkm8   1/1     Running   0          14m   10.0.1.15    ip-10-0-1-101.ec2.internal
sample-api-7b8f9c6d4b-9qx7p   1/1     Running   0          14m   10.0.1.42    ip-10-0-1-102.ec2.internal
sample-api-7b8f9c6d4b-lm4tz   1/1     Running   0          14m   10.0.1.88    ip-10-0-1-103.ec2.internal
```

### 2. Correlate Nodes with Availability Zone Labels
```bash
kubectl get nodes -L topology.kubernetes.io/zone,kubernetes.io/hostname
```

*Illustrative Output:*
```text
NAME                          STATUS   ROLES    AGE   VERSION   ZONE
ip-10-0-1-101.ec2.internal    Ready    <none>   5d    v1.31.0   us-east-1a
ip-10-0-1-102.ec2.internal    Ready    <none>   5d    v1.31.0   us-east-1a
ip-10-0-1-103.ec2.internal    Ready    <none>   5d    v1.31.0   us-east-1a
ip-10-0-2-201.ec2.internal    Ready    <none>   5d    v1.31.0   us-east-1b
ip-10-0-3-301.ec2.internal    Ready    <none>   5d    v1.31.0   us-east-1c
```

**Diagnostic Finding:** 100% of workload replicas are concentrated on nodes in `us-east-1a`. Zero replicas reside in `us-east-1b` or `us-east-1c`.

---

## 5. Root Cause

1. **Absence of Topology Policy:** The Deployment specification defined `spec.replicas: 3`, but omitted `spec.template.spec.topologySpreadConstraints`.
2. **Resource-Driven Scoring Bias:** The scheduler's scoring plugins selected nodes in `us-east-1a` based on resource utilization or bin-packing criteria without any constraint requiring zone distribution.
3. **Misalignment Between Redundancy and Placement:** The architecture relied on an abstract idea of "high availability" via replica count, rather than designing placement against an explicit failure domain.

---

## 6. Design Mitigation

We configure declarative topology spread constraints targeting `topology.kubernetes.io/zone`:

```yaml
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app.kubernetes.io/name: sample-api
```

### Intended Distribution Across Zones

Under this constraint, the scheduler distributes the 3 replicas evenly across the 3 available zones:

```text
Zone us-east-1a ───► Pod A (Ready)
Zone us-east-1b ───► Pod B (Ready)
Zone us-east-1c ───► Pod C (Ready)
```

If `us-east-1a` becomes completely unavailable:
- Pod A becomes unavailable.
- Pod B (`us-east-1b`) and Pod C (`us-east-1c`) **survive** outside the failed failure domain.
- 2 out of 3 replicas remain active to absorb client traffic.

> [!CAUTION]
> **Surviving Replicas $\ne$ Application Availability:**
> Do not convert 2/3 surviving replicas into a claim of "66% application availability." Surviving replicas do not automatically guarantee application availability. Remaining compute capacity, readiness, traffic routing, downstream dependencies (e.g., databases), and stateful connections all dictate whether the application can successfully serve traffic during a zone outage.

---

## 7. Trade-offs Introduced

Introducing hard topology spread constraints solves correlated placement risk, but creates a new architectural trade-off:
- **Scheduling Inelasticity:** The scheduler will reject candidate nodes in populated zones even if they have abundant compute resources.
- **The Pending Pod Boundary:** If worker nodes in the target zone are exhausted or unavailable, new pods remain `Pending`. This operational boundary is examined in [Unsatisfiable Topology Spread](unsatisfiable-topology-spread.md).
