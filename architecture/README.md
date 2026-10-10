# Architecture Overview

This directory contains the system models, architectural decision records (ADRs), and visual diagrams governing the design of the **Production EKS Platform**.

Platform engineering is fundamentally about managing failure boundaries, state transitions, and operational trade-offs. The documentation in this section provides the technical reasoning behind our platform configuration.

---

## Contents

- **[Diagrams](diagrams/)**
  - [Workload Health & Traffic Flow](diagrams/workload-health-flow.md): Demonstrates the decoupling between node-level kubelet control signals (probes) and the client request data plane (Ingress/Service/EndpointSlices).
  - [Planned Disruption Flow & Eviction](diagrams/planned-disruption-flow.md): Demonstrates how the Eviction API interacts with PodDisruptionBudgets during node drain operations.
  - [Multi-AZ Topology Resilience](diagrams/topology-failure-domain-flow.md): Demonstrates how bounded skew constraints isolate Availability Zone failure domains.
  - [Stateful Workload Recovery & Identity](diagrams/stateful-recovery-flow.md): Demonstrates how deterministic ordinal identity, headless DNS, and dedicated storage survive Pod termination.
  - [Secret Trust-Boundary & Delivery Flow](diagrams/secret-trust-boundary-flow.md): Demonstrates the four system planes, RBAC retrieval boundaries, workload delivery mechanisms, and downstream application leakage channels.
- **[Architecture Decision Records (ADR)](adr/)**
  - [ADR-001: Kubernetes Health Probe Strategy](adr/ADR-001-kubernetes-health-probe-strategy.md): Defines distinct semantics for startup, readiness, and liveness probes.
  - [ADR-002: PodDisruptionBudget Strategy](adr/ADR-002-pod-disruption-budget-strategy.md): Evaluates disruption thresholds, unhealthy pod eviction policies, and maintenance boundaries.
  - [ADR-003: Topology Resilience Strategy Across Multi-AZ Failure Domains](adr/ADR-003-topology-resilience-strategy.md): Evaluates placement mechanics, bounded skew invariants, and failure domain isolation.
  - [ADR-004: Stateful Workload Strategy & Persistent Identity Model](adr/ADR-004-stateful-workload-strategy.md): Evaluates ordinal identity, Headless Services, volume claim templates, and storage recovery boundaries.
  - [ADR-005: Kubernetes Secret Handling & Trust-Boundary Strategy](adr/ADR-005-kubernetes-secret-handling-strategy.md): Evaluates native secret storage, Base64 representation limits, RBAC resource scoping, volume mount delivery, and rotation boundaries before external secret managers.
