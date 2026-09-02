# Architecture Overview

This directory contains the system models, architectural decision records (ADRs), and visual diagrams governing the design of the **Production EKS Platform**.

Platform engineering is fundamentally about managing failure boundaries, state transitions, and operational trade-offs. The documentation in this section provides the technical reasoning behind our platform configuration.

---

## Contents

- **[Diagrams](diagrams/)**
  - [Workload Health & Traffic Flow](diagrams/workload-health-flow.md): Demonstrates the decoupling between node-level kubelet control signals (probes) and the client request data plane (Ingress/Service/EndpointSlices).
- **[Architecture Decision Records (ADR)](adr/)**
  - [ADR-001: Kubernetes Health Probe Strategy](adr/ADR-001-kubernetes-health-probe-strategy.md): Defines distinct semantics for startup, readiness, and liveness probes.
