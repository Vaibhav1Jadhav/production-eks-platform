# Planned Disruption Scenarios & Hands-On Lab

This directory explores how Kubernetes workloads behave during planned maintenance events (such as worker node drains, automated AMI rotations, and autoscaler node consolidations), demonstrating the boundaries of policy-aware evictions and the operational role of `PodDisruptionBudget` (PDB).

---

## Core Operational Principle

$$
\text{PodDisruptionBudget} \ne \text{Application High Availability}
$$

A `PodDisruptionBudget` defines how much voluntary disruption an already resilient workload can tolerate at any given moment. It does **not** create application availability by itself.

---

## Voluntary vs. Involuntary Disruption

Understanding Kubernetes disruption requires separating two fundamentally different failure classes:

```text
                        ┌─────────────────────────────────────────────────────────┐
                        │                 Kubernetes Disruption                   │
                        └────────────────────────────┬────────────────────────────┘
                                                     │
                     ┌───────────────────────────────┴───────────────────────────────┐
                     ▼                                                               ▼
       ┌───────────────────────────┐                                   ┌───────────────────────────┐
       │   Voluntary Disruption    │                                   │  Involuntary Disruption   │
       └─────────────┬─────────────┘                                   └─────────────┬─────────────┘
                     │                                                               │
     Initiated by operator or automation                             Initiated by physical/hardware faults
     Uses the Eviction API (/eviction)                               Bypasses all control plane APIs
     Evaluated against PodDisruptionBudget                           CANNOT be prevented by a PDB
                     │                                                               │
     Examples:                                                       Examples:
     • kubectl drain <node>                                          • Worker node kernel panic / crash
     • AWS EKS Managed Node Group AMI updates                        • Underlying AWS EC2 hypervisor failure
     • Karpenter / Cluster Autoscaler consolidation                  • Out-Of-Memory (OOM) killer terminating pod
     • Workload eviction via resource pressure                       • Direct pod deletion (kubectl delete pod)
```

---

## Scenarios & Hands-On Lab Index

| Document | Focus Area | Key Takeaway |
| :--- | :--- | :--- |
| **[Hands-On Lab: Can This Workload Survive a Node Drain?](node-drain-lab.md)** | Step-by-Step Controlled Experiment | Practical verification on a disposable cluster showing eviction pacing, probe warm-up, and recovery. |
| **[Failure Scenario 1: Unprotected Node Drain](unprotected-node-drain.md)** | Insufficient Disruption Protection | Concurrent drains without a PDB drop active serving capacity below baseline, causing client request timeouts. |
| **[Failure Scenario 2: Over-Restrictive PDB](over-restrictive-pdb.md)** | Cluster Maintenance Deadlock | Setting `minAvailable` equal to total replicas drops `disruptionsAllowed` to 0, halting node drains and patching. |

---

## Platform Architecture Cross-References

- **[Planned Disruption Flow Architecture](../../architecture/diagrams/planned-disruption-flow.md):** Control flow of Eviction API evaluation, replacement scheduling, and readiness restoration.
- **[ADR-002: PodDisruptionBudget Strategy](../../architecture/adr/ADR-002-pod-disruption-budget-strategy.md):** Architectural decision record detailing why `minAvailable: 2` and `unhealthyPodEvictionPolicy: AlwaysAllow` were selected.
- **[Runbook: Planned Node Disruption & Stuck Drain Investigation](../../runbooks/planned-node-disruption.md):** Step-by-step diagnostic and remediation guide for platform engineers when node drains hang.
- **[Sample API Workload Manifests](../../kubernetes/workloads/sample-api/):** Declarative implementation containing the Deployment, Service, and PodDisruptionBudget.
