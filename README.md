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

Our platform design establishes clean boundaries between node-level control plane health signals, runtime client request data paths, planned infrastructure maintenance, and physical multi-AZ failure domains:

- **[Workload Health & Traffic Flow Architecture](architecture/diagrams/workload-health-flow.md)**: Visualizes how `kubelet` evaluates container lifecycle vs. how `Ingress`, `Service`, and `EndpointSlices` route user traffic. Demonstrates the foundational principle:
  $$\text{Container Running} \ne \text{Application Ready}$$
- **[Planned Disruption Flow & Eviction Architecture](architecture/diagrams/planned-disruption-flow.md)**: Visualizes how the Kubernetes Eviction API interacts with `PodDisruptionBudget` during planned node maintenance (`kubectl drain`), separating policy-aware eviction from direct deletion and Deployment rolling updates. Demonstrates the reliability principle:
  $$\text{PodDisruptionBudget} \ne \text{Application High Availability}$$
- **[Multi-AZ Topology Resilience & Failure Domain Architecture](architecture/diagrams/topology-failure-domain-flow.md)**: Visualizes how `kube-scheduler` enforces bounded skew (`maxSkew: 1`, `DoNotSchedule`) across Availability Zones (`topology.kubernetes.io/zone`), isolating single-zone infrastructure failure while modeling the hard placement pending boundary. Demonstrates the reliability principle:
  $$\text{Replica Count} \ne \text{Placement Resilience}$$
- **[Stateful Workload Recovery & Identity Architecture](architecture/diagrams/stateful-recovery-flow.md)**: Visualizes how the StatefulSet controller re-establishes deterministic ordinal identity, headless DNS addressing, and dedicated PersistentVolumeClaim association across Pod crashes, contrasting successful recovery against the storage attachment and mount failure boundary. Demonstrates the reliability principle:
  $$\text{Pod Replacement} \ne \text{State Recovery}$$
- **[Secret Trust-Boundary & Delivery Flow Architecture](architecture/diagrams/secret-trust-boundary-flow.md)**: Visualizes the four system planes (control, storage, runtime, observability), contrasting least-privilege RBAC scoping against broad collection verbs, evaluating in-memory `tmpfs` volume projection vs. environment variables, and identifying downstream application leakage channels. Demonstrates the security principle:
  $$\text{Base64 Encoding} \ne \text{Secret Management}$$

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

### 3. Multi-AZ Workload Resilience & Topology Spread Strategy
- **Failure Domain Isolation (`topology.kubernetes.io/zone`):** Binds replica distribution to physical AWS Availability Zones, ensuring replicas do not silently pack into a single physical data center.
- **Bounded Distribution (`maxSkew: 1`):** Restricts zone skew to $\le 1$, ensuring even spread across zones during steady-state and horizontal scaling ($1/1/1$, $2/1/1$, $2/2/1$).
- **Hard Invariant Enforcement (`whenUnsatisfiable: DoNotSchedule`):** Refuses candidate nodes that violate the skew constraint, prioritizing explicit failure domain isolation over unconstrained placement.
- **Strict Selector Alignment:** Targets `app.kubernetes.io/name: sample-api` matching the existing workload selector.
- **Production Manifest:** Configured under `spec.template.spec` in [`kubernetes/workloads/sample-api/deployment.yaml`](kubernetes/workloads/sample-api/deployment.yaml).

### 4. Stateful Workload Identity & Persistent State Recovery
- **Stable Ordinal Identity (`stateful-demo-0`, `stateful-demo-1`):** Non-fungible replicas with deterministic ordinal naming, enabling direct clustering, Raft consensus, and shard targeting.
- **Deterministic Network Identity (Headless Service):** Configures `clusterIP: None` to delegate direct A/AAAA DNS record resolution to CoreDNS (`<pod-name>.stateful-demo-headless.sample-workloads.svc.cluster.local`), eliminating virtual IP load balancing for peer-to-peer coordination.
- **Dedicated Storage Association (`volumeClaimTemplates`):** Automatically provisions independent PersistentVolumeClaims (`data-stateful-demo-0`) bound via `ReadWriteOnce`. Claims persist independently of Pod lifecycle, automatically reattaching to replacement pods upon failure.
- **Storage Topology & Dynamic Provisioning:** Aligns with multi-AZ topology by requiring `WaitForFirstConsumer` volume binding, ensuring backing block storage (e.g., AWS EBS) is provisioned in the same physical Availability Zone as the scheduled compute node.
- **Data-Safety PVC Retention:** Maintains default `Retain` policy to ensure neither automated scaling nor accidental StatefulSet deletion destroys underlying persistent data.
- **Production Kubernetes Manifests:** Configured in [`kubernetes/workloads/stateful-demo/`](kubernetes/workloads/stateful-demo/).

### 5. Kubernetes Secret Trust-Boundary & Delivery Strategy
- **Base64 Representation Boundary:** Distinguishes data representation (RFC 4648) from cryptographic confidentiality, demonstrating that Base64 confers zero encryption or access control.
- **Least-Privilege RBAC Scoping:** Binds workload ServiceAccount strictly to named secrets using `resourceNames: ["demo-app-secret"]` and narrow verb `get`, rejecting wildcard `*` or collection-level `list`/`watch` permissions that expand blast radius across the namespace.
- **In-Memory Volume Projection:** Delivers secrets via `tmpfs` volume mounts with strict permissions (`defaultMode: 0400`, `readOnly: true`), ensuring credentials reside in node RAM rather than persistent block storage.
- **Safe Manifest Hygiene:** Rejects committed raw/Base64 credentials in Git, using synthetic placeholders (`<DEMO_SECRET_VALUE>`) in declarative manifests and documenting out-of-band imperative provisioning for local clusters.
- **Production Kubernetes Manifests:** Configured in [`kubernetes/workloads/secret-demo/`](kubernetes/workloads/secret-demo/).

---

## Architecture Decision Records (ADR)

- **[ADR-001: Kubernetes Health Probe Strategy](architecture/adr/ADR-001-kubernetes-health-probe-strategy.md)**  
  Documents the decision to establish distinct, isolated semantics for startup, readiness, and liveness probes, alternatives evaluated (unified `/health`, downstream coupling, readiness only), and engineering trade-offs.
- **[ADR-002: PodDisruptionBudget Strategy](architecture/adr/ADR-002-pod-disruption-budget-strategy.md)**  
  Evaluates mathematical trade-offs for voluntary disruption thresholds (`minAvailable: 2` vs `minAvailable: 1` vs `100%`), unhealthy eviction policies (`AlwaysAllow` vs `IfHealthyBudget`), and failure boundaries.
- **[ADR-003: Workload Topology Resilience Strategy Across Multi-AZ Failure Domains](architecture/adr/ADR-003-topology-resilience-strategy.md)**
  Evaluates placement mechanisms across multi-AZ failure domains, comparing topology spread constraints (`DoNotSchedule` vs `ScheduleAnyway`) against binary pod anti-affinity, analyzing `minDomains`, and defining safe diagnostic boundaries.
- **[ADR-004: Stateful Workload Strategy & Persistent Identity Model](architecture/adr/ADR-004-stateful-workload-strategy.md)**
  Evaluates architectural trade-offs between stateless Deployments, shared network filesystems, and StatefulSets with volume claim templates; analyzes single-node mount semantics (`ReadWriteOnce`), storage topology binding modes (`Immediate` vs `WaitForFirstConsumer`), PVC retention vs PV reclaim policy boundaries, and failure isolation.
- **[ADR-005: Kubernetes Secret Handling & Trust-Boundary Strategy](architecture/adr/ADR-005-kubernetes-secret-handling-strategy.md)**
  Evaluates native secret storage, Base64 representation limits, RBAC resource scoping, volume mount delivery vs environment variables, and rotation boundaries before external secret managers.

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

### Topology & Multi-AZ Resilience (Milestone #23)
- **[Topology Failure Scenarios](failure-scenarios/topology/README.md)**:
  - **[Scenario A: Correlated Zone Placement Risk](failure-scenarios/topology/correlated-zone-placement.md)**: Demonstrates how replica redundancy without topology constraints leaves workloads vulnerable to complete outage during single-zone physical impairments.
  - **[Scenario B: Unsatisfiable Topology Spread & The Pending Pod Boundary](failure-scenarios/topology/unsatisfiable-topology-spread.md)**: Dissects the operational boundary of `DoNotSchedule`, differentiating topology skew constraints from node compute exhaustion, taints, and missing zone labels.

### Stateful Workload Recovery (Milestone #24)
- **[Stateful Recovery Scenarios & Hands-On Lab](failure-scenarios/stateful/README.md)**:
  - **[Scenario 1: Pod Replacement with Persistent State](failure-scenarios/stateful/pod-replacement-with-persistent-state.md)**: Demonstrates that while a pod is destroyed, its ordinal identity, bound PVC, and persisted application data survive, verifying the complete recovery chain.
  - **[Scenario 2: Volume Attachment & Mount Failure](failure-scenarios/stateful/volume-attachment-or-mount-failure.md)**: Explores the second failure boundary where a replacement pod is created, but remains unserviceable due to multi-attach locks, storage detach timeouts, or zone topology mismatches.
  - **[Hands-On Lab: Verifying Stateful Recovery](failure-scenarios/stateful/stateful-recovery-lab.md)**: Controlled step-by-step procedure on a disposable cluster verifying data persistence across pod termination.

### Kubernetes Secrets & Trust Boundaries (Milestone #25)
- **[Secret Failure Scenarios & Hands-On Lab](failure-scenarios/secrets/README.md)**:
  - **[Scenario 1: Unauthorized Secret Access](failure-scenarios/secrets/unauthorized-secret-access.md)**: Diagnoses RBAC authorization boundaries and API HTTP 403 Forbidden rejections using `kubectl auth can-i`.
  - **[Scenario 2: Over-Permissive Secret Access](failure-scenarios/secrets/over-permissive-secret-access.md)**: Explores how broad `list`/`watch` permissions expand blast radius across the namespace, contrasting against `resourceNames` least privilege.
  - **[Scenario 3: Secret Leakage Outside Kubernetes](failure-scenarios/secrets/secret-leakage-outside-kubernetes.md)**: Analyzes how application runtimes leak credentials into stdout logs and APM telemetry despite secure platform delivery.
  - **[Scenario 4: Secret Rotation Lifecycle Boundary](failure-scenarios/secrets/secret-rotation-lifecycle-boundary.md)**: Dissects the desynchronization trap between kubelet atomic filesystem symlink updates and application in-memory credential caching.
  - **[Hands-On Lab: Secret Security & Trust-Boundary Verification](failure-scenarios/secrets/secret-security-lab.md)**: Controlled step-by-step verification on a disposable cluster.

---

## Operational Runbooks

- **[Runbook: Pod Running but Users Receive 5xx](runbooks/pod-running-but-5xx.md)**  
  Hypothesis-driven, outside-in diagnostic workflow:
  $$\text{Client} \longrightarrow \text{Gateway} \longrightarrow \text{Service} \longrightarrow \text{EndpointSlice} \longrightarrow \text{Pod Readiness} \longrightarrow \text{Probes} \longrightarrow \text{Application}$$
- **[Runbook: Planned Node Disruption & Stuck Node Drain Investigation](runbooks/planned-node-disruption.md)**  
  Outside-in diagnostic procedure for isolating blocked node drains:
  $$\text{Node Drain} \longrightarrow \text{Eviction Denial} \longrightarrow \text{PDB Status} \longrightarrow \text{Replica Readiness} \longrightarrow \text{Scheduling Capacity} \longrightarrow \text{Safe Recovery}$$
- **[Runbook: Topology Scheduling Failure & Pending Pod Investigation](runbooks/topology-scheduling-failure.md)**
  Evidence-based diagnostic procedure for isolating stuck `Pending` pods:
  $$\text{Pod Pending} \longrightarrow \text{Scheduler Events} \longrightarrow \text{Zone Topology Labels} \longrightarrow \text{Replica Skew Audit} \longrightarrow \text{Failure Domain Classification} \longrightarrow \text{Safe Recovery}$$
- **[Runbook: Stateful Workload Recovery & Storage Attachment Investigation](runbooks/stateful-workload-recovery.md)**
  Outside-in diagnostic hierarchy tracing stateful recovery:
  $$\text{Replica Unhealthy} \longrightarrow \text{Ordinal Identity} \longrightarrow \text{Scheduler} \longrightarrow \text{PVC Status} \longrightarrow \text{PV/StorageClass} \longrightarrow \text{Attach/Mount} \longrightarrow \text{Application State} \longrightarrow \text{Readiness}$$
- **[Runbook: Secret Access, Mounting & Delivery Failure Investigation](runbooks/secret-access-and-delivery-failure.md)**
  Outside-in diagnostic hierarchy tracing credential failures:
  $$\text{Credential Failure} \longrightarrow \text{Workload Identity} \longrightarrow \text{Secret Object} \longrightarrow \text{RBAC Authorization} \longrightarrow \text{tmpfs Mount} \longrightarrow \text{File Mode 0400} \longrightarrow \text{Memory Freshness}$$

---

## Production DevOps Insights

This repository serves as the practical evidence layer for the LinkedIn series **Production DevOps Insights**:

### Volume 1: Kubernetes Workload & Node Reliability

| Milestone | Topic | Question / Scenario | Implementation Status | Technical Evidence |
| :--- | :--- | :--- | :--- | :--- |
| [**#21**](https://lnkd.in/p/dWUXSe5t) | [Kubernetes Probes — Running ≠ Ready](https://lnkd.in/p/dWUXSe5t) | *"Your Pod Is Running. Why Are Users Still Getting 5xx?"* | **Implementation available** | [Workload Manifests](kubernetes/workloads/sample-api/) • [ADR-001](architecture/adr/ADR-001-kubernetes-health-probe-strategy.md) • [Runbook](runbooks/pod-running-but-5xx.md) • [Failure Scenarios](failure-scenarios/probes/) |
| **#22** | PodDisruptionBudget — Surviving Planned Disruption | *"Can This Workload Survive a Node Drain?"* | **Implementation available** *(Pending publication)* | [PDB Manifest](kubernetes/workloads/sample-api/poddisruptionbudget.yaml) • [ADR-002](architecture/adr/ADR-002-pod-disruption-budget-strategy.md) • [Planned Disruption Flow](architecture/diagrams/planned-disruption-flow.md) • [Node Drain Lab](failure-scenarios/disruptions/node-drain-lab.md) • [Runbook](runbooks/planned-node-disruption.md) • [Failure Scenarios](failure-scenarios/disruptions/) |
| **#23** | Multi-AZ / Topology Resilience | *"Will Another Replica Survive the Failure Domain We Designed For?"* | **Implementation available** *(LinkedIn publication pending)* | [Deployment Manifest](kubernetes/workloads/sample-api/deployment.yaml) • [ADR-003](architecture/adr/ADR-003-topology-resilience-strategy.md) • [Topology Architecture](architecture/diagrams/topology-failure-domain-flow.md) • [Runbook](runbooks/topology-scheduling-failure.md) • [Failure Scenarios](failure-scenarios/topology/) |
| **#24** | Stateful Workloads — Pod Replacement ≠ State Replacement | *"Can the Workload Recover with the Correct Identity and State?"* | **Implementation available** *(LinkedIn publication pending)* | [StatefulSet Manifests](kubernetes/workloads/stateful-demo/) • [ADR-004](architecture/adr/ADR-004-stateful-workload-strategy.md) • [Stateful Architecture](architecture/diagrams/stateful-recovery-flow.md) • [Runbook](runbooks/stateful-workload-recovery.md) • [Failure Scenarios](failure-scenarios/stateful/) |

### Volume 2: Security, Delivery, GitOps & IaC

| Milestone | Topic | Question / Scenario | Implementation Status | Technical Evidence |
| :--- | :--- | :--- | :--- | :--- |
| **#25** | Kubernetes Secrets — Base64 ≠ Secret Management | *"Who Can Retrieve the Credential, Where Does It Live, and Where Can It Leak?"* | **Implementation available** *(LinkedIn publication pending)* | [Workload Manifests](kubernetes/workloads/secret-demo/) • [ADR-005](architecture/adr/ADR-005-kubernetes-secret-handling-strategy.md) • [Secret Trust-Boundary Flow](architecture/diagrams/secret-trust-boundary-flow.md) • [Runbook](runbooks/secret-access-and-delivery-failure.md) • [Failure Scenarios](failure-scenarios/secrets/) |

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
