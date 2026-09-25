# Topology Failure Scenarios & Multi-AZ Resilience

This directory explores how Kubernetes workloads behave across physical infrastructure failure domains (Availability Zones), contrasting the correlated placement risks that occur when explicit topology constraints are absent with the new operational boundaries introduced by hard topology spread constraints.

Engineering thesis:

$$
\text{Replica Count} = \text{Redundancy}
$$

$$
\text{Placement} = \text{Fault Survival}
$$

> "Replica count provides redundancy. Placement determines which failures that redundancy can survive."
> "Design topology against an explicit failure domain—not an abstract idea of high availability."

---

## The Two Faces of Topology Resilience

Topology management in Kubernetes requires balancing two opposing failure risks:

```text
                           ┌─────────────────────────────────────────────────────────┐
                           │               Topology Placement Dynamics               │
                           └────────────────────────────┬────────────────────────────┘
                                                        │
                        ┌───────────────────────────────┴───────────────────────────────┐
                        ▼                                                               ▼
          ┌───────────────────────────┐                                   ┌───────────────────────────┐
          │ Correlated Placement Risk │                                   │ Unsatisfiable Constraint  │
          │  (No Topology Constraints)│                                   │   (DoNotSchedule Hard)    │
          └─────────────┬─────────────┘                                   └─────────────┬─────────────┘
                        │                                                               │
        Scheduler packs pods based solely                               Scheduler strictly refuses to place
        on available node compute resources.                            pods if candidate placement violates skew.
                        │                                                               │
        Result: Replicas can co-locate in                               Result: Pods remain stuck in Pending
        a single Availability Zone.                                     if eligible zone capacity is depleted.
                        │                                                               │
        Failure Mode: Single AZ outage takes                            Failure Mode: Workload cannot scale or
        down 100% of workload replicas.                                 replace pods until zone capacity recovers.
```

---

## Scenario Index

| Document | Focus Area | Core Insight |
| :--- | :--- | :--- |
| **[Scenario 1: Correlated Zone Placement Risk](correlated-zone-placement.md)** | Missing Topology Constraints | Replica count alone does not guarantee multi-AZ survival. If all replicas land in one zone, a single facility outage takes down the entire service tier. |
| **[Scenario 2: Unsatisfiable Topology Spread](unsatisfiable-topology-spread.md)** | Hard Constraint (`DoNotSchedule`) Boundary | When the scheduler cannot satisfy `maxSkew: 1`, pods remain `Pending`. Operators must distinguish topology constraints from resource deficits, taints, and label defects. |

---

## Evidence Classification Reference

All scenarios in this directory use the repository's strict evidence standards:

- **DESIGNED:** Expected system behavior derived from architecture design and documented Kubernetes scheduling semantics.
- **STATICALLY VALIDATED:** Configuration schemas and manifest invariants verified by local automated test suites.
- **OBSERVED:** Runtime behavior verified on a running multi-node/multi-zone Kubernetes cluster with captured events.
- **SKIPPED:** Environmental or runtime validations not executed due to absence of multi-AZ cluster infrastructure.

---

## Platform Architecture Cross-References

- **[ADR-003: Topology Resilience Strategy](../../architecture/adr/ADR-003-topology-resilience-strategy.md):** Architectural decision record detailing `maxSkew: 1`, `topologyKey: topology.kubernetes.io/zone`, and `DoNotSchedule`.
- **[Topology Architecture Flow Diagram](../../architecture/diagrams/topology-failure-domain-flow.md):** Mermaid visualization of topology distribution, zone loss survival, and the hard constraint pending boundary.
- **[Runbook: Topology Scheduling Failure & Pending Pod Investigation](../../runbooks/topology-scheduling-failure.md):** Outside-in operational diagnostic workflow for isolating stuck `Pending` pods.
- **[Sample API Workload Manifests](../../kubernetes/workloads/sample-api/):** Declarative implementation containing the Deployment, Service, and PodDisruptionBudget.
