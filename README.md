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

Our platform design establishes clean boundaries between node-level control plane health signals, runtime client request data paths, and planned infrastructure maintenance:

- **[Workload Health & Traffic Flow Architecture](architecture/diagrams/workload-health-flow.md)**: Visualizes how `kubelet` evaluates container lifecycle vs. how `Ingress`, `Service`, and `EndpointSlices` route user traffic. Demonstrates the foundational principle:
  $$\text{Container Running} \ne \text{Application Ready}$$
- **[Planned Disruption Flow & Eviction Architecture](architecture/diagrams/planned-disruption-flow.md)**: Visualizes how the Kubernetes Eviction API interacts with `PodDisruptionBudget` during planned node maintenance (`kubectl drain`), separating policy-aware eviction from direct deletion and Deployment rolling updates. Demonstrates the reliability principle:
  $$\text{PodDisruptionBudget} \ne \text{Application High Availability}$$

---

## Implemented Capabilities

### 1. Workload Health Strategy & Probe Architecture
- **Startup Protection (`startupProbe`):** Guards slow application warm-ups and cache populating without inflating liveness timeouts or triggering crash loops.
- **Traffic Isolation (`readinessProbe`):** Decouples client traffic eligibility from container restarts. Readiness failure marks the corresponding endpoint as not ready (`ready: false`), ensuring normal Kubernetes Service traffic does not select it as a ready backend.
- **Deadlock Recovery (`livenessProbe`):** Restricts container restarts to fatal internal hangs. Liveness should generally avoid depending on shared downstream services whose failure cannot be repaired by restarting this container, preventing restart storms that amplify outages.
- **Reproducible Reference Workload:** Minimal, zero-dependency Python service exposing dedicated `/startup`, `/ready`, `/health`, and traffic endpoints with controllable failure injection flags in [`examples/sample-api/`](examples/sample-api/).
- **Production Kubernetes Manifests:** Declarative workload definition configured with Pod Security Standards (`restricted`), non-root user, dropped capabilities, and read-only root filesystem in [`kubernetes/workloads/sample-api/`](kubernetes/workloads/sample-api/).

### 2. Planned Disruption Management & PodDisruptionBudget Strategy
- **Calibrated Disruption Budget (`minAvailable: 2`):** Guarantees a minimum serving floor during voluntary maintenance. Scaled to 3 replicas to demonstrate single-replica voluntary disruption tolerance while 2 ready replicas remain online to absorb baseline traffic.
- **Eviction API Policy Boundary:** Enforces that voluntary disruptions (`kubectl drain`, node consolidation) respect workload capacity, while explicitly documenting that involuntary crashes and Deployment rollouts are not constrained by PDBs.
- **Unhealthy Pod Eviction Policy (`AlwaysAllow`):** Adopts Kubernetes v1.31+ stable policy allowing unhealthy/unready pods to be evicted during maintenance, preventing faulty applications from permanently trapping worker node OS and kernel patching.
- **Hands-On Controlled Lab:** Step-by-step reproducible experiment on a disposable cluster demonstrating eviction pacing, probe warm-up, and over-restrictive budget deadlocks in [`failure-scenarios/disruptions/node-drain-lab.md`](failure-scenarios/disruptions/node-drain-lab.md).
- **Production Manifest:** Configured in [`kubernetes/workloads/sample-api/poddisruptionbudget.yaml`](kubernetes/workloads/sample-api/poddisruptionbudget.yaml).

---

## Architecture Decision Records (ADR)

- **[ADR-001: Kubernetes Health Probe Strategy](architecture/adr/ADR-001-kubernetes-health-probe-strategy.md)**  
  Documents the decision to establish distinct, isolated semantics for startup, readiness, and liveness probes, alternatives evaluated (unified `/health`, downstream coupling, readiness only), and engineering trade-offs.
- **[ADR-002: PodDisruptionBudget Strategy](architecture/adr/ADR-002-pod-disruption-budget-strategy.md)**  
  Evaluates mathematical trade-offs for voluntary disruption thresholds (`minAvailable: 2` vs `minAvailable: 1` vs `100%`), unhealthy eviction policies (`AlwaysAllow` vs `IfHealthyBudget`), and failure boundaries.

---

## Failure Scenarios

### Workload Probes (Milestone #21)
- **[Scenario A: Premature Readiness (Running ≠ Ready)](failure-scenarios/probes/premature-readiness.md)**  
  Deep-dive into how a pod in the `Running` phase causes HTTP 5xx errors for end users when readiness is evaluated prematurely during rollouts or scaling.
- **[Scenario B: Aggressive Liveness as an Outage Amplifier](failure-scenarios/probes/aggressive-liveness.md)**  
  Analysis of how aggressive liveness checks can turn transient local slowdowns, startup delays, or incorrectly coupled dependency failures into repeated restarts and reduced availability.

### Planned Disruptions (Milestone #22)
- **[Disruption Scenarios & Hands-On Lab](failure-scenarios/disruptions/README.md)**:
  - **[Scenario A: Unprotected Node Drain](failure-scenarios/disruptions/unprotected-node-drain.md)**: Demonstrates how draining nodes without a PDB causes rapid simultaneous terminations, endpoint depletion, and request timeouts.
  - **[Scenario B: Over-Restrictive PDB](failure-scenarios/disruptions/over-restrictive-pdb.md)**: Explores how setting `minAvailable == replicas` drops `disruptionsAllowed` to 0, causing Eviction API HTTP 429 rejections and freezing infrastructure upgrades.
  - **[Hands-On Lab: Can This Workload Survive a Node Drain?](failure-scenarios/disruptions/node-drain-lab.md)**: Controlled node drain testing procedure on a disposable cluster.

---

## Operational Runbooks

- **[Runbook: Pod Running but Users Receive 5xx](runbooks/pod-running-but-5xx.md)**  
  Hypothesis-driven, outside-in diagnostic workflow:
  $$\text{Client} \longrightarrow \text{Gateway} \longrightarrow \text{Service} \longrightarrow \text{EndpointSlice} \longrightarrow \text{Pod Readiness} \longrightarrow \text{Probes} \longrightarrow \text{Application}$$
- **[Runbook: Planned Node Disruption & Stuck Node Drain Investigation](runbooks/planned-node-disruption.md)**  
  Outside-in diagnostic procedure for isolating blocked node drains:
  $$\text{Node Drain} \longrightarrow \text{Eviction Denial} \longrightarrow \text{PDB Status} \longrightarrow \text{Replica Readiness} \longrightarrow \text{Scheduling Capacity} \longrightarrow \text{Safe Recovery}$$

---

## Production DevOps Insights

This repository serves as the practical evidence layer for the LinkedIn series **Production DevOps Insights**:

| Milestone | Topic | Question / Scenario | Implementation Status | Technical Evidence |
| :--- | :--- | :--- | :--- | :--- |
| [**#21**](https://lnkd.in/p/dWUXSe5t) | [Kubernetes Probes — Running ≠ Ready](https://lnkd.in/p/dWUXSe5t) | *"Your Pod Is Running. Why Are Users Still Getting 5xx?"* | **Implementation available** | [Workload Manifests](kubernetes/workloads/sample-api/) • [ADR-001](architecture/adr/ADR-001-kubernetes-health-probe-strategy.md) • [Runbook](runbooks/pod-running-but-5xx.md) • [Failure Scenarios](failure-scenarios/probes/) |
| **#22** | PodDisruptionBudget — Surviving Planned Disruption | *"Can This Workload Survive a Node Drain?"* | **Implementation available** *(Pending publication)* | [PDB Manifest](kubernetes/workloads/sample-api/poddisruptionbudget.yaml) • [ADR-002](architecture/adr/ADR-002-pod-disruption-budget-strategy.md) • [Planned Disruption Flow](architecture/diagrams/planned-disruption-flow.md) • [Node Drain Lab](failure-scenarios/disruptions/node-drain-lab.md) • [Runbook](runbooks/planned-node-disruption.md) • [Failure Scenarios](failure-scenarios/disruptions/) |

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
