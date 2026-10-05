# Stateful Workload Failure Scenarios: Identity & Persistent Recovery

This directory explores how stateful workloads behave under Pod loss, node failure, and storage attachment constraints, contrasting the seamless replacement of stateless replicas with the failure boundaries of persistent state.

Engineering thesis:

$$
\text{Stateless Replica} \longrightarrow \text{Replaceable}
$$

$$
\text{Stateful Replica} \longrightarrow \text{Recoverable (Identity + State)}
$$

> "A stateless replica can often be replaced. A stateful replica may need to be recovered with the correct identity and the correct data."
>
> "Pod replacement is not the same as state recovery."

---

## The Two Failure Boundaries of Stateful Workloads

When a stateless replica crashes, Kubernetes spawns a replacement pod that immediately begins serving traffic once healthy. When a stateful replica crashes, recovery spans two distinct architectural boundaries:

```text
                             ┌─────────────────────────────────────────────────────────┐
                             │              Stateful Replica Disruption                │
                             └────────────────────────────┬────────────────────────────┘
                                                          │
                         ┌────────────────────────────────┴────────────────────────────────┐
                         ▼                                                                 ▼
           ┌───────────────────────────┐                                     ┌───────────────────────────┐
           │   Boundary 1: Identity    │                                     │    Boundary 2: Storage    │
           │  Recreation & Scheduling  │                                     │   Attachment & Recovery   │
           └─────────────┬─────────────┘                                     └─────────────┬─────────────┘
                         │                                                                 │
         StatefulSet controller recreates                                  The replacement pod exists, but
         the missing ordinal (stateful-demo-0).                            cannot access its persistent state.
                         │                                                                 │
         Result: Replacement pod is created                                Result: Pod is trapped in
         and scheduled to an eligible node.                                ContainerCreating or CrashLoopBackOff.
                         │                                                                 │
         Failure Mode: Missing capacity or                                 Failure Mode: Volume locked to old node,
         zone topology mismatch leaves pod Pending.                        mount failure, or corrupt application data.
```

---

## Scenario Index

| Document | Focus Area | Core Insight | Status |
| :--- | :--- | :--- | :--- |
| **[Scenario 1: Pod Replacement with Persistent State](pod-replacement-with-persistent-state.md)** | Ordinal Identity & PVC Reassociation | The pod is destroyed, but its identity and persistent data survive. The replacement pod reattaches the existing claim, incrementing historical state markers. | **DESIGNED / STATICALLY VALIDATED** |
| **[Scenario 2: Volume Attachment & Mount Failure](volume-attachment-or-mount-failure.md)** | Multi-Attach & Storage Boundaries | A replacement pod is created, but remains unserviceable due to storage locks, zone detachment delays, or mount failures. Pod created $\ne$ state recovered. | **DESIGNED / ILLUSTRATIVE** |
| **[Hands-On Lab: Verifying Stateful Recovery](stateful-recovery-lab.md)** | Controlled Verification Procedure | Step-by-step reproducible lab procedure on a disposable cluster to write state, terminate an ordinal pod, and verify data persistence across replacement. | **DESIGNED / PROCEDURE READY** |

---

## Evidence Classification Reference

All scenarios in this directory adhere strictly to the repository's evidence standards:

- **DESIGNED:** Expected architectural behavior derived from Kubernetes core controller semantics and CSI specifications.
- **STATICALLY VALIDATED:** Configuration syntax, label alignments, probe definitions, and manifest integrity verified locally by `scripts/validate.sh`.
- **ILLUSTRATIVE:** Representative logs and event signatures demonstrating diagnostic patterns without fabricating execution against live infrastructure.
- **OBSERVED:** Runtime verification captured from a live Kubernetes cluster with verifiable output.
- **SKIPPED:** Real-world runtime execution was skipped (e.g., when no live cluster with dynamic CSI volume provisioning was connected).
