# Production EKS Platform

A production-oriented reference implementation for progressively building and operating Kubernetes/EKS platform capabilities through architecture decisions, reproducible implementations, failure scenarios, operational runbooks, security controls, and engineering trade-offs.

This repository evolves incrementally alongside real-world architectural patterns and failure modes. It prioritizes concrete evidence, operational clarity, and disciplined engineering over speculative tooling or theoretical frameworks.

---

## Purpose

The primary purpose of this platform repository is to serve as the **evidence layer** for platform engineering and Kubernetes operational practices. While architectural concepts and post-mortems can be discussed theoretically, production reliability requires verifiable configuration, reproducible failure modes, diagnostic runbooks, and explicit trade-off analyses.

Each capability added to this repository reflects a practical production problem, demonstrating not only how the component is constructed, but how it behaves under stress, how it fails, and how operators diagnose and recover it.

---

## Engineering Principles

1. **Evidence over claims**  
   Capabilities are backed by reproducible configurations, testable workloads, and observable failure modes—never unverified assertions of scale or reliability.

2. **Failure-oriented design**  
   Systems are evaluated by how they degrade, recover, and isolate faults, rather than solely how they operate under optimal conditions.

3. **Architecture before tooling**  
   Operational patterns and failure boundaries dictate tool selection, not ecosystem popularity or checklist compliance.

4. **Explicit trade-offs**  
   Every engineering decision incurs costs (complexity, performance, cognitive overhead). Architectural choices must document what is sacrificed alongside what is gained.

5. **Reproducible validation**  
   Configurations and scripts must be verifiable locally or in controlled environments with clear pass/fail criteria.

6. **Security by default**  
   Least-privilege access, secret exclusion, read-only root filesystems, and minimal container surfaces are baked into initial manifests, not retrofitted.

7. **Incremental platform evolution**  
   The platform grows milestone by milestone. Empty directories, speculative scaffolds, and premature enterprise abstractions are prohibited.

---

## Architecture

Our platform design establishes a clean boundary between node-level control plane health signals and runtime client request data paths:

- **[Workload Health & Traffic Flow Architecture](architecture/diagrams/workload-health-flow.md)**: Visualizes how `kubelet` evaluates container lifecycle vs. how `Ingress`, `Service`, and `EndpointSlices` route user traffic. Demonstrates the foundational principle:
  $$\text{Container Running} \ne \text{Application Ready}$$

---

## Implemented Capabilities

### 1. Workload Health Strategy & Probe Architecture
- **Startup Protection (`startupProbe`):** Guards slow application warm-ups and cache populating without inflating liveness timeouts or triggering crash loops.
- **Traffic Isolation (`readinessProbe`):** Decouples client traffic eligibility from container restarts. Readiness failure marks the corresponding endpoint as not ready (`ready: false`), ensuring normal Kubernetes Service traffic does not select it as a ready backend.
- **Deadlock Recovery (`livenessProbe`):** Restricts container restarts to fatal internal hangs. Liveness should generally avoid depending on shared downstream services whose failure cannot be repaired by restarting this container, preventing restart storms that amplify outages.
- **Reproducible Reference Workload:** Minimal, zero-dependency Python service exposing dedicated `/startup`, `/ready`, `/health`, and traffic endpoints with controllable failure injection flags in [`examples/sample-api/`](examples/sample-api/).
- **Production Kubernetes Manifests:** Declarative workload definition configured with Pod Security Standards (`restricted`), non-root user, dropped capabilities, and read-only root filesystem in [`kubernetes/workloads/sample-api/`](kubernetes/workloads/sample-api/).

---

## Architecture Decision Records (ADR)

- **[ADR-001: Kubernetes Health Probe Strategy](architecture/adr/ADR-001-kubernetes-health-probe-strategy.md)**  
  Documents the decision to establish distinct, isolated semantics for startup, readiness, and liveness probes, alternatives evaluated (unified `/health`, downstream coupling, readiness only), and engineering trade-offs.

---

## Failure Scenarios

- **[Scenario A: Premature Readiness (Running ≠ Ready)](failure-scenarios/probes/premature-readiness.md)**  
  Deep-dive into how a pod in the `Running` phase causes HTTP 5xx errors for end users when readiness is evaluated prematurely during rollouts or scaling.
- **[Scenario B: Aggressive Liveness as an Outage Amplifier](failure-scenarios/probes/aggressive-liveness.md)**  
  Analysis of how aggressive liveness checks can turn transient local slowdowns, startup delays, or incorrectly coupled dependency failures into repeated restarts and reduced availability.

---

## Operational Runbooks

- **[Runbook: Pod Running but Users Receive 5xx](runbooks/pod-running-but-5xx.md)**  
  Hypothesis-driven, outside-in diagnostic workflow:
  $$\text{Client} \longrightarrow \text{Gateway} \longrightarrow \text{Service} \longrightarrow \text{EndpointSlice} \longrightarrow \text{Pod Readiness} \longrightarrow \text{Probes} \longrightarrow \text{Application}$$

---

## Production DevOps Insights

This repository serves as the practical evidence layer for the LinkedIn series **Production DevOps Insights**:

| Milestone | Topic | Question / Scenario | Implementation Status | Technical Evidence |
| :--- | :--- | :--- | :--- | :--- |
| [**#21**](https://lnkd.in/p/dWUXSe5t) | [Kubernetes Probes — Running ≠ Ready](https://lnkd.in/p/dWUXSe5t) | *"Your Pod Is Running. Why Are Users Still Getting 5xx?"* | **Implementation available** | [Workload Manifests](kubernetes/workloads/sample-api/) • [ADR-001](architecture/adr/ADR-001-kubernetes-health-probe-strategy.md) • [Runbook](runbooks/pod-running-but-5xx.md) • [Failure Scenarios](failure-scenarios/probes/) |

---

## Repository Evolution

Future platform capabilities will be introduced incrementally across core areas:

- **Kubernetes Reliability** (Graceful termination, PodDisruptionBudgets, topology spread constraints)
- **Security** (Workload identity, NetworkPolicies, secret lifecycle management)
- **Infrastructure as Code** (Modular baseline provisioning, reproducible infrastructure patterns)
- **GitOps** (Declarative reconciliation, progressive delivery)
- **Observability** (SLIs/SLOs, Prometheus metrics, distributed tracing)
- **SRE & Operations** (Hypothesis-driven runbooks, failure injection)
- **FinOps** (Workload rightsizing, capacity allocation)
- **Platform Engineering** (Developer golden paths, self-service abstractions)

*Capabilities are added to this list only as concrete implementations and evidence are merged.*

---

## Local Validation

Run the local validation test suite to verify YAML manifests, probe semantics, and security hygiene:

```bash
./scripts/validate.sh
```
