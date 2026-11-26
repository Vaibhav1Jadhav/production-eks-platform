# ADR-006: External Secrets Synchronization & Application Adoption Strategy

## Status

Accepted

---

## Context

In Milestone #25, we established the Kubernetes-native secret trust boundaries across storage (etcd), API authorization (RBAC), and node delivery (`tmpfs` volume projections vs. process environment variables). We demonstrated that Base64 encoding is mere data representation rather than cryptographic confidentiality, and that committing secret manifests to Git violates core security hygiene.

However, native Kubernetes Secrets do not solve the authoritative secret lifecycle:
1. **Source of Truth:** Where do production credentials originate, who manages rotation schedules, and how are secrets secured with hardware-backed encryption (KMS)?
2. **Dynamic Ingestion:** How do credentials flow into the cluster without human operators executing imperative `kubectl create secret` commands or CI/CD pipelines possessing broad cluster administrative access?
3. **The Synchronization vs. Adoption Boundary:** When an external credential rotates, how does the system propagate that change from the cloud provider down to the running application process?

A pervasive operational trap in cloud-native platforms is:

$$
\text{ExternalSecret Ready} = \text{Kubernetes Secret Updated} \ne \text{Application Process Adopted} \ne \text{Downstream Auth Successful}
$$

To establish a resilient enterprise secret delivery model on AWS EKS, we must evaluate how external credentials are synchronized into Kubernetes and how application runtimes consume them.

---

## Problem Statement

A production database password or third-party API token rotates in an authoritative external secret store.

The platform relies on an operator to reconcile external state into Kubernetes. If the controller updates the target Kubernetes `kind: Secret` object, engineers frequently assume:

> *"The ExternalSecret status reports `SecretSynced=True`. The Kubernetes Secret has been updated. The deployment is healthy."*

This assumption hides critical lifecycle failure boundaries:
1. **The Delivery Plane Freeze (Environment Variables):** If the workload consumes credentials via environment variables (`env[].valueFrom.secretKeyRef`), the Linux kernel resolves those variables during container process initialization (`execve()`). Updating the underlying Kubernetes Secret has **zero effect** on the running container's environment. The container process continues executing with the stale, revoked credential indefinitely until restarted.
2. **The Volume Mount Symlink Gap & `subPath` Trap:** If delivered via standard projected volume mounts, kubelet eventually updates files on `tmpfs` via symlink rotation (`..data`), but running applications that cache credentials into long-lived connection pools will not reload them without file watching (`inotify`) or a restart. Crucially, if a secret is mounted using `subPath`, **automated updates are completely disabled by Kubernetes**, freezing the mounted file to its initial boot-time state.
3. **The Adoption vs. Downstream Auth Gap:** Even if an application successfully reloads a new credential into memory, this does not prove the downstream datastore or service accepts it (e.g., misconfigured permissions, network partitions, or delayed replication).
4. **The Premature Revocation Hazard:** If the database administrator revokes the old password immediately after updating the external store, every application replica enters a cascade of authentication failures before the operator synchronizes the secret or before workloads restart.

We must select an external secrets architecture that provides automated synchronization while establishing deterministic operational procedures for credential adoption and revocation.

---

## Decision

We establish the following architectural policies for External Secrets Management:

### 1. Standardize on External Secrets Operator (ESO) v0.9.11 with AWS Secrets Manager
We adopt **External Secrets Operator (ESO) v0.9.11** with the `external-secrets.io/v1beta1` CRD API specification:
- Operates as a declarative, in-cluster Kubernetes controller that reconciles external provider state into native Kubernetes Secret objects.
- Uses **AWS Secrets Manager** as the primary managed AWS/EKS secret backend, leveraging KMS envelope encryption and CloudTrail auditing.
- Supports configurable polling and reconciliation intervals (`refreshInterval: 1h`) to detect upstream credential rotations.

### 2. Backend Roles: Primary, Alternatives, and Exclusions
We document distinct secret backend options as alternative options, not three mandatory components in one deployment:
- **AWS Secrets Manager (Primary Managed Backend):** The selected production implementation for AWS EKS workloads. Fully integrated via IAM Roles for Service Accounts (IRSA) with native AWS KMS encryption.
- **OpenBao (Self-Hosted Secrets Management Backend):** An open-source, community-driven fork of HashiCorp Vault under the Linux Foundation. Evaluated as an alternative self-hosted backend.
  - *Compatibility with ESO v0.9.11:* The pinned ESO v0.9.11 CRD schema (`external-secrets.io/v1beta1`) has **no dedicated native `spec.provider.openbao` provider**. While OpenBao maintains Vault v1/v2 HTTP API compatibility (allowing generic interaction via ESO's `spec.provider.vault` provider pointing to an OpenBao server endpoint), native OpenBao provider CRDs, dedicated authentication methods, and integration testing of OpenBao-specific engine features were not officially packaged in ESO v0.9.11. Compatibility with Vault does not automatically prove every OpenBao integration works without dedicated end-to-end verification. Consequently, OpenBao is documented as a viable alternative self-hosted backend and a future compatibility-tested extension; we do not fabricate working manifests.
- **HashiCorp Vault (Alternative Secrets Management Backend):** An alternative enterprise secret engine supporting dynamic database secrets, lease revocation, and transit encryption, under the HashiCorp BSL license. Adds operational overhead of unseal keys, Raft storage, and cluster maintenance.
- **Explicit Exclusion of Vaultwarden:**
  - **Vaultwarden is strictly excluded from the ESO architecture.**
  - Vaultwarden is a lightweight, Bitwarden-compatible password manager server intended for personal and organizational end-user credential vaults.
  - Vaultwarden is **not** an equivalent Kubernetes workload-secret backend.
  - Vaultwarden is **not** a policy engine, **not** a Kubernetes secret controller, and **not** an application credential rotation solution.
  - It provides no External Secrets Operator provider integration and has no role in automated workload credential delivery.

### 3. Isolate Controller Identity from Workload Identity via EKS IRSA
- The ESO controller authenticates to AWS Secrets Manager using **IAM Roles for Service Accounts (IRSA)** or **EKS Pod Identity**.
- The dedicated controller ServiceAccount (`eso-irsa-sa`) is annotated with the IAM Role ARN.
- Workload application pods use an unprivileged ServiceAccount (`external-secret-app-sa`) that possesses **zero AWS IAM permissions**, preventing workloads from querying cloud provider APIs directly.
- **Access Boundary Realities:**
  - Removing AWS IAM permissions prevents cloud API traversal, but does **not** prevent compromise of the Kubernetes Secret delivered to the workload container. If the workload is compromised (e.g. via RCE), its delivered credentials can still be exfiltrated.
  - Identities with pod creation or exec permissions (`pods/create`, `pods/exec`) in the namespace can mount or extract secrets. While Pod Security Standards (PSS) enforce execution security properties (e.g., non-root user, dropped capabilities), **PSS alone does not prevent authorized Pod creators from referencing Secrets**. Complete isolation requires combining IAM scoping, namespace RBAC, workload authorization design, and fine-grained admission policies (e.g., Kyverno, Gatekeeper, ValidatingAdmissionPolicy).

### 4. Enforce Namespaced `SecretStore` Boundaries
- Workloads must use namespaced `kind: SecretStore` resources rather than cluster-wide `ClusterSecretStore` resources, unless multi-tenant sharing is explicitly approved.
- A namespaced `SecretStore` establishes a localized configuration scope within the namespace.
- **Tenant Isolation Nuance:** A namespaced `SecretStore` does **not** create a complete or impermeable security boundary on its own. Effective multi-tenant isolation requires coordinated enforcement of AWS IAM policy scoping on the projected IRSA role, Kubernetes RBAC governing `secrets` and `externalsecrets`, admission policies, and node/workload isolation.

### 5. Require Declarative `ExternalSecret` with Owner Policy
- `ExternalSecret` manifests explicitly declare `spec.target.creationPolicy: Owner`. If the `ExternalSecret` is deleted, its generated Kubernetes Secret is garbage-collected.
- Credentials map directly to specific JSON properties or raw strings using `spec.data[].remoteRef`.

### 6. Verify the Complete 6-Stage Credential Lifecycle
Because process environment variables are immutable at runtime, `subPath` mounts never update, and in-memory application connection pools do not automatically reload, **credential rotation is not complete upon Secret synchronization**.

The complete lifecycle consists of six distinct stages:

```text
External Secret Backend (AWS Secrets Manager / Vault / OpenBao)
        │ (1. Upstream rotation / version staging)
        ▼
External Secrets Operator (ESO Controller)
        │ (2. IRSA / Pod Identity authentication, poll loop)
        ▼
Kubernetes Secret (Target in etcd)
        │ (3. etcd persistence, resourceVersion mutation)
        ▼
Pod Credential Delivery Plane
        ├── Path A: Environment Variables (FROZEN at container execve; requires pod restart)
        ├── Path B: Projected Volume Mount (Kubelet ..data symlink swap; requires app reload)
        └── Path C: Mounted via subPath (FROZEN; kubelet NEVER updates subPath mounts)
        ▼
Application Reload / Restart / Credential Adoption Plane
        │ (5. In-memory heap, connection pool refresh, or new pod boot)
        ▼
Downstream Authentication Plane
        │ (6. Datastore verifies credential; old credential revoked safely)
```

#### Key Lifecycle Behaviors:
1. **Environment variables are established at container start:** The Linux kernel initializes environment tables once during container process creation (`execve()`). Updating the Kubernetes Secret does not mutate the environment of an already-running process.
2. **Secret volume projections update eventually, but the application must re-read the updated file:** Kubelet periodically rotates the `..data` symlink on `tmpfs` volume projections (typically 60-90s). However, application processes that read configuration into static in-memory variables or connection pools at boot will not observe the new secret without file-watching logic (`inotify`) or a restart.
3. **A Secret mounted using `subPath` does not receive automated updates:** When a Secret volume is mounted into a container using `subPath` or `subPathExpr`, kubelet binds directly to the specific file inode at mount time, completely bypassing symlink projection. `subPath` volume mounts **never** receive automated updates when the Secret changes.
4. **ESO Ready confirms controller synchronization, not application adoption:** An `ExternalSecret` condition of `Ready=True` and `SecretSynced` indicates only that the controller successfully mirrored the external payload into Kubernetes etcd. It asserts nothing about running process state.
5. **Successful application adoption still does not prove downstream authentication:** Even if an application reads the updated credential from disk or boots fresh with updated environment variables, downstream authentication can still fail due to database permission errors, account lockouts, network firewalls, or delayed database replication.
6. **Old credential revocation must be coordinated with verified adoption and downstream authentication:** Revoking old credentials before 100% of workload replicas have adopted the new credential and verified downstream authentication causes catastrophic cluster-wide authentication outages.

---

## Architectural Trust Boundaries

```text
[ External Secret Backend: AWS Secrets Manager ]
          │ (1. Provider Plane: Authoritative Key-Value Store & KMS Encryption)
          ▼
[ External Secrets Operator ] ─── (2. Controller Identity: EKS IRSA / Pod Identity)
          │
          │ (3. Synchronization Plane: Polling loop, secretStoreRef, reconcile)
          ▼
[ Kubernetes kind: Secret ] ─── (4. Cluster Storage: etcd persistence)
          │
          ├── (5A. Volume Projection: tmpfs symlink swap; requires app reload)
          ├── (5B. Environment Variable: Immutable at execve; requires pod restart)
          └── (5C. subPath Mount: Inode-bound; NEVER updated by kubelet)
          ▼
[ Application Process ] ─── (6. Adoption Plane: In-memory heap / connection pool)
          │
          ▼
[ Downstream Datastore ] ─── (7. Downstream Auth Plane: Validates active credential)
```

### Boundary 1: Authoritative Provider vs. Cluster State
- AWS Secrets Manager manages rotation schedules, KMS envelope encryption, and IAM audit logs. (OpenBao and HashiCorp Vault represent alternative self-hosted/enterprise backends).
- The Kubernetes Secret object is a localized, downstream mirror. The mirror can experience synchronization latency bounded by `spec.refreshInterval`.

### Boundary 2: Controller IAM vs. Workload Identity
- ESO requires `secretsmanager:GetSecretValue` and `secretsmanager:DescribeSecret` scoped strictly to target secret ARNs (`production/app/database-*`).
- The application pod requires zero AWS permissions, preventing direct AWS Secrets Manager API queries. However, the workload still receives the native Kubernetes Secret in its environment, meaning workload compromise exposes its delivered credentials locally. Furthermore, Kubernetes `pods/create` and `pods/exec` permissions in the namespace can access secrets unless constrained by RBAC and fine-grained admission policies (since Pod Security Standards govern execution posture, not Secret-reference authorization).

### Boundary 3: Synchronization vs. Runtime Adoption
- ESO reconciling `SecretSynced=True` only confirms that the Kubernetes Secret `.data` field matches the external provider.
- It does **not** confirm that running pods have loaded the new credential or that the downstream datastore accepts it.

---

## Alternatives Considered

```mermaid
flowchart TD
    subgraph OPTIONS["Secret Management Architectural Options"]
        A["1. Native Kubernetes Secrets\n(Static YAML / CI push)"]
        B["2. Secrets Store CSI Driver\n(Direct Pod Volume Mount)"]
        C["3. Direct In-App AWS SDK\n(Application queries Secrets Manager)"]
        D["4. HashiCorp Vault\n(Dynamic Lease Engine / Alternative)"]
        E["5. OpenBao\n(Self-Hosted OSS / Future Extension)"]
        F["6. Vaultwarden\n(Password Manager) [EXCLUDED]"]
        G["7. External Secrets Operator (ESO) + AWS SM\n(Declarative K8s Controller) [SELECTED]"]
    end
```

### Alternative 1: Native Kubernetes Secrets (Static Manifests / CI Injection)
- **Description:** Injecting credentials into Kubernetes Secrets via CI/CD pipelines during deployment.
- **Evaluation:** **REJECTED for enterprise secret management.**
  - Requires CI/CD runners to hold broad Kubernetes administrative privileges to create/update secrets.
  - No automated detection of out-of-band rotations initiated in the authoritative secret backend.
  - Secret drift occurs between the authoritative cloud store and the cluster.

### Alternative 2: Secrets Store CSI Driver (Mount-Based Direct Projection)
- **Description:** Using the Kubernetes Secrets Store CSI Driver with the AWS Secrets Manager provider to mount secrets directly into pods as volumes.
- **Evaluation:** **VIABLE BUT REJECTED FOR GENERAL WORKLOADS.**
  - *Advantages:* Never writes secrets to etcd unless `syncSecret: true` is configured; secrets exist solely on node `tmpfs`.
  - *Disadvantages:* Tightly couples Pod startup to CSI driver availability and AWS API latency. If AWS Secrets Manager throttles API calls during a massive scale-out event, pod startup blocks (`ContainerCreating`). Does not support environment variables natively. Adds substantial daemonset complexity across worker nodes.

### Alternative 3: Direct In-App AWS SDK Calls
- **Description:** Application microservices embed the AWS SDK and call `GetSecretValue` at startup.
- **Evaluation:** **REJECTED.**
  - *Advantages:* Eliminates all Kubernetes Secret objects and etcd persistence entirely.
  - *Disadvantages:* Tightly couples application code to AWS APIs, breaking vendor independence and complicating local testing/mocking. Every application pod requires AWS IAM permissions. During large autoscaling surges, hundreds of pods hammer AWS Secrets Manager simultaneously, causing API rate limit exhaustion and exorbitant AWS API request costs.

### Alternative 4: HashiCorp Vault (Dynamic Leases)
- **Description:** Deploying HashiCorp Vault with dynamic database credentials and short-lived leases.
- **Evaluation:** **VIABLE ALTERNATIVE / SPECIALIZED.**
  - *Advantages:* Best-in-class security with short-lived dynamic credentials, lease revocation, and automated database user provisioning.
  - *Disadvantages:* Significant operational burden. Requires operating a highly available Vault cluster (or paying substantial HCP Vault SaaS fees), managing unseal keys, complex Raft storage, and specialized operational runbooks. For teams whose primary infrastructure is AWS EKS, native AWS Secrets Manager backed by ESO provides 90% of the benefit with a fraction of the operational overhead.

### Alternative 5: OpenBao (Self-Hosted Open-Source Backend)
- **Description:** Deploying OpenBao (Linux Foundation open-source fork of Vault) as a self-hosted secrets backend.
- **Evaluation:** **VIABLE ALTERNATIVE / FUTURE EXTENSION.**
  - *Advantages:* True open-source licensing (MPL-2.0), no BSL licensing restrictions, Vault API compatibility.
  - *Disadvantages for Pinned ESO v0.9.11:* ESO v0.9.11 has no native `spec.provider.openbao` CRD schema. While basic KV operations may function through the generic `vault` provider, native authentication workflows and full feature compatibility require future verification. Documented as an alternative self-hosted option rather than fabricating unverified manifests.

### Alternative 6: Vaultwarden
- **Description:** Lightweight Bitwarden-compatible password manager server.
- **Evaluation:** **EXCLUDED FROM ARCHITECTURE.**
  - Vaultwarden is a user-facing credential manager designed for personal/team password vaults and browser extension synchronization.
  - It is **not** a Kubernetes workload-secret backend, **not** a policy engine, **not** a Kubernetes secret controller, and **not** an application credential rotation solution.
  - It has no provider API in ESO and is out of scope for workload secret delivery.

---

## Trade-offs & Consequences

### What We Gain
1. **Authoritative Cloud Source:** Secrets originate in AWS Secrets Manager with KMS envelope encryption, automatic rotation schedules, and AWS CloudTrail auditability.
2. **Declarative GitOps Compatibility:** Manifests commit safely to Git (`SecretStore` and `ExternalSecret`) with zero credential exposure. The actual `kind: Secret` is generated dynamically by the controller.
3. **Decoupled Identity:** Application workloads have no access to AWS IAM credentials or Secrets Manager APIs.
4. **Resilient Local Caching:** Because ESO creates a native Kubernetes Secret, pods can restart, scale out, or reschedule during transient AWS Secrets Manager API outages without blocking startup.
5. **Clear Backend Distinction:** AWS Secrets Manager is the primary managed implementation, with OpenBao and Vault evaluated as distinct alternative backends (not three co-dependent requirements).

### What We Incur
1. **Controller Dependency:** The platform introduces the External Secrets Operator as a critical cluster dependency. If the ESO controller crashes, existing secrets remain, but rotations stop reconciling.
2. **Sync Latency Window:** Credential changes in AWS are not instantaneous; they propagate according to `spec.refreshInterval` (e.g., up to 1 hour unless manually triggered).
3. **Adoption Responsibility:** The platform must enforce rolling deployment rollouts on rotation. Updating the Kubernetes Secret does not update running application memory, process environment variables, or `subPath` volume mounts.
4. **Revocation Discipline:** Teams should enforce a dual-credential overlap window at the authoritative database whenever simultaneous valid credentials are supported; for single-credential systems, teams must plan coordinated maintenance windows or brief rotation restart cycles to prevent outages while pods cycle.
