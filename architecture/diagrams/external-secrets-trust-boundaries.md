# External Secrets Trust Boundaries & Synchronization Architecture

Secret synchronization is not application credential adoption.

When an external credential manager like AWS Secrets Manager updates a password, the change traverses six distinct system planes and seven boundary checks before client traffic successfully authenticates. A failure at any single boundary stops the rotation dead, even if every preceding layer reports a green status.

```text
External Secret Backend
        ↓
External Secrets Operator
        ↓
Kubernetes Secret
        ↓
Pod Credential Delivery (Env Var vs Volume vs subPath)
        ↓
Application Reload / Restart / Credential Adoption
        ↓
Downstream Authentication
```

$$
\text{ExternalSecret Ready} \ne \text{Application Credential Adopted} \ne \text{Downstream Auth Successful}
$$

---

## 1. The End-to-End Credential Delivery Lifecycle

```mermaid
flowchart TD
    subgraph BACKEND_PLANE["1. External Secret Backend Plane"]
        direction TB
        SM[("AWS Secrets Manager (Primary)\nSecret: production/app/database\nVersion: AWSCURRENT (v2)")]
        KMS["AWS KMS\nCustomer Master Key (CMK)"]
        SM -.->|"Envelope Encryption"| KMS
        ALT["Alternative Backends:\n• OpenBao (Self-Hosted OSS / Future Ext)\n• HashiCorp Vault (Dynamic Leases)\n[EXCLUDED: Vaultwarden (User Password Mgr)]"]
    end

    subgraph AUTH_PATH["2. Controller Authentication Plane (IRSA)"]
        direction TB
        ESO_SA["ESO ServiceAccount\neso-irsa-sa"]
        OIDC["EKS OIDC Identity Provider"]
        STS["AWS STS\nAssumeRoleWithWebIdentity"]
        IAM_Role["IAM Role\neso-secrets-manager-role"]

        ESO_SA -->|"Projected ServiceAccount JWT"| OIDC
        OIDC -->|"Federated Web Identity"| STS
        STS -->|"Temporary AWS Credentials"| IAM_Role
        IAM_Role -->|"secretsmanager:GetSecretValue"| SM
    end

    subgraph SYNC_PLANE["3. Kubernetes Control Plane (Reconciliation)"]
        direction TB
        Store["SecretStore / Namespaced\naws-secrets-manager-store"]
        ExtSec["ExternalSecret Controller Loop\napp-database-credential-sync\n(Status: SecretSynced=True)"]
        K8sSec[("Kubernetes Secret (Target)\napp-database-secret\nresourceVersion: 98124")]

        Store --> ExtSec
        ExtSec -->|"Reconciles (refreshInterval: 1h)"| SM
        ExtSec -->|"Creates / Mutates"| K8sSec
    end

    subgraph DELIVERY_PLANE["4. Workload Delivery Plane"]
        direction TB
        PathA["Path A: Environment Variable\nvalueFrom.secretKeyRef\n(FROZEN AT CONTAINER EXECVE)"]
        PathB["Path B: Volume Mount Projection\nspec.volumes.secret\n(ASYNC ..data SYM-LINK SWAP)"]
        PathC["Path C: Volume Mount subPath\nvolumeMounts.subPath\n(FROZEN; KUBELET NEVER UPDATES)"]
    end

    subgraph APP_PLANE["5. Application Runtime Plane"]
        direction TB
        ProcMem["Running Container Process\nIn-Memory Heap / Connection Pool\n(Holds cached password from boot)"]
        RestartPod["Replacement Pod (Restarted)\n(Reads updated credential at boot)"]
    end

    subgraph AUTH_PLANE["6. Downstream Service Plane"]
        direction TB
        Database[("Production Database\nPostgreSQL / Aurora")]
        AuthStatus{"Authentication Decision"}

        ProcMem -.->|"Queries with OLD_CRED (v1)"| Database
        RestartPod -->|"Queries with NEW_CRED (v2)"| Database
        Database --> AuthStatus
        AuthStatus -.->|"If old revoked: FATAL 401/5xx"| Failure["Outage Cascade"]
        AuthStatus -->|"If new accepted: 200 OK"| Success["Healthy Traffic"]
    end

    K8sSec --> PathA
    K8sSec --> PathB
    K8sSec --> PathC
    PathA -.->|"Injected at boot (STALE)"| ProcMem
    PathA -->|"Injected after rollout (ADOPTED)"| RestartPod
    PathC -.->|"Mounted at boot (STALE)"| ProcMem
```

---

## 2. Backend Roles & Architectural Scope

Our implementation establishes AWS Secrets Manager as the primary managed backend for AWS/EKS workloads, while clearly delineating alternative backends and strictly excluding non-workload tools:

1. **AWS Secrets Manager (Primary Managed Backend):**
   - Authoritative AWS-managed key-value store with KMS envelope encryption and CloudTrail auditing.
   - Pinned integration uses External Secrets Operator v0.9.11 with `external-secrets.io/v1beta1` and EKS IAM Roles for Service Accounts (IRSA).
2. **OpenBao (Self-Hosted Backend Option):**
   - An open-source, community-governed fork of HashiCorp Vault under the Linux Foundation (MPL-2.0).
   - *Compatibility with ESO v0.9.11:* The pinned ESO v0.9.11 release contains **no dedicated native `openbao` provider** in its CRD schema. While OpenBao provides Vault v1/v2 HTTP API compatibility (enabling potential connectivity via the generic `vault` provider), official SecretStore schema support, dedicated authentication plugins, and verified feature parity were not packaged in ESO v0.9.11. Compatibility with HashiCorp Vault does not automatically prove every OpenBao integration works without dedicated end-to-end testing. OpenBao is documented as an alternative self-hosted backend and a future compatibility-tested extension; we do not fabricate unverified manifests.
3. **HashiCorp Vault (Alternative Enterprise Backend):**
   - An alternative secrets management engine supporting dynamic database secrets, lease revocation, and transit encryption.
   - Incurs significant operational overhead (unseal keys, Raft consensus storage, high-availability cluster maintenance).
   - *Clarification:* AWS Secrets Manager, OpenBao, and HashiCorp Vault are **alternative backend options**, not three co-dependent components in a single deployment.
4. **Explicit Exclusion of Vaultwarden:**
   - **Vaultwarden is strictly excluded from the ESO architecture.**
   - Vaultwarden is a lightweight, Bitwarden-compatible password manager server intended for personal/team user password storage and browser extension synchronization.
   - It is **not** an equivalent Kubernetes workload-secret backend.
   - It is **not** a policy engine, **not** a Kubernetes secret controller, and **not** an application credential rotation solution.
   - It has no provider API in ESO and is out of scope for automated workload credential delivery.

---

## 3. Detailed Trust Boundary Analysis

### Boundary 1: Authoritative Provider Availability & Versioning
- **Location:** AWS Secrets Manager.
- **Responsibility:** Houses the single source of truth for the sensitive credential. Controls version staging (`AWSCURRENT`, `AWSPREVIOUS`, custom version stages) and KMS envelope encryption.
- **Failure Mode:** AWS API throttling (`ThrottlingException`), regional endpoint failure, or missing KMS decrypt permissions (`kms:Decrypt`).
- **Boundary Truth:** A healthy credential in AWS Secrets Manager does not mean Kubernetes is aware of it. Reconciliation happens on a periodic schedule (`refreshInterval`).

---

### Boundary 2: Controller IAM Authentication & Authorization (EKS IRSA)
- **Location:** EKS OIDC Provider $\to$ AWS STS $\to$ AWS IAM.
- **Responsibility:** Grants the External Secrets Operator controller temporary AWS credentials without storing long-lived static AWS Access Keys in the cluster.
- **Mechanism:**
  1. Kubelet injects a projected service account JWT token into the ESO controller pod.
  2. The AWS SDK in the ESO pod calls `sts:AssumeRoleWithWebIdentity`, presenting the projected JWT.
  3. STS validates the JWT against the EKS cluster's OIDC discovery endpoint.
  4. STS returns temporary AWS access credentials scoped to the IAM role.
- **Least-Privilege Scoping:**
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "AllowReadAppDatabaseSecret",
        "Effect": "Allow",
        "Action": [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ],
        "Resource": "arn:aws:secretsmanager:us-east-1:123456789012:secret:production/app/database-*"
      }
    ]
  }
  ```
- **Boundary Truth:** If the IAM role trust policy does not match the exact ServiceAccount namespace and name, or if the role lacks `secretsmanager:GetSecretValue` on the specific secret ARN, ESO fails with `AccessDeniedException`. While workload application pods are denied access to this cloud IAM role, they still possess local access to the Kubernetes Secret delivered to their container. Compromise of the workload exposes its own delivered credentials, and namespace users with pod-creation or exec privileges can inspect secrets unless constrained by RBAC and fine-grained admission policies (as Pod Security Standards govern execution posture, not Secret-reference authorization).

---

### Boundary 3: The Reconciliation Loop (`SecretStore` & `ExternalSecret`)
- **Location:** Kubernetes Control Plane (`kube-apiserver` & ESO Controller).
- **Responsibility:** Evaluates `ExternalSecret` specifications against the configured `SecretStore`.
- **Tenant Scope:** A namespaced `kind: SecretStore` establishes a localized configuration scope within `sample-workloads`. It does not independently guarantee complete multi-tenant isolation without corresponding IAM policy resource scoping, RBAC governance, and workload admission controls.
- **Reconciliation Invariants:**
  - `SecretStore` defines the connection parameters (AWS service, region, authentication reference).
  - `ExternalSecret` defines the refresh cadence (`refreshInterval: 1h`), the target Kubernetes Secret name (`target.name`), and the mapping from external keys to secret keys.
- **Boundary Truth:** When the reconciliation loop succeeds, ESO sets:
  ```yaml
  status:
    conditions:
      - type: Ready
        status: "True"
        reason: SecretSynced
  ```
  This status proves **only** that the controller successfully fetched the payload from AWS and wrote it into the Kubernetes `kind: Secret` object in etcd. It asserts nothing about workload health.

---

### Boundary 4: Workload Credential Delivery (Env Var vs. Volume Mount vs. subPath)
- **Location:** Kubelet daemon & Container Runtime on the worker node.
- **The Three Delivery Paths:**

| Delivery Mechanism | Initial Injection | In-Flight Secret Mutation Behavior | Operational Consequence |
| :--- | :--- | :--- | :--- |
| **Environment Variable (`secretKeyRef`)** | Resolved once during container process `execve()`. | **Completely frozen.** The Linux kernel does not update environment tables of running processes. | Running containers retain the old credential until the pod is terminated and replaced. |
| **Volume Mount Projection (`volumes[].secret`)** | Mounted on node memory `tmpfs`. | **Asynchronously updated.** Kubelet swaps the `..data` symlink within 60–90 seconds. | File on disk updates, but application process memory does not update unless an `inotify` watcher is implemented. |
| **Mounted via `subPath` (`volumeMounts[].subPath`)** | Bound directly to file inode on `tmpfs`. | **Completely frozen.** Kubernetes bypasses symlink tracking for `subPath` mounts; updates are **never** delivered. | File on disk remains frozen to initial boot-time snapshot; requires full pod restart to update. |

---

### Boundary 5: Application Credential Adoption
- **Location:** Application Process Virtual Memory (Heap, Connection Pool, Environment Table).
- **Responsibility:** The running code must read the new credential and apply it to downstream connections.
- **The Staleness Trap:**
  - Database connection pools (e.g., HikariCP, pgpool, SQLAlchemy) initialize database connections during startup using the credentials available in memory at boot time.
  - If a secret rotates in AWS Secrets Manager and synchronizes to the Kubernetes Secret object, the application process never re-reads its environment variables or `subPath` files.
  - The process continues executing with the old credential until explicitly restarted via `kubectl rollout restart`.

---

### Boundary 6: Downstream Authentication & The Revocation Window
- **Location:** The Target Service (PostgreSQL, MySQL, Redis, External SaaS API).
- **Responsibility:** Accepts or rejects the authentication request.
- **The Premature Revocation Hazard:**
  - **Rotation** is the introduction and propagation of a new credential.
  - **Revocation** is the deactivation of the old credential.
  - An overlap period where both old and new credentials remain valid is the preferred strategy **when the downstream service supports simultaneous valid credentials**.
  - In single-credential environments where simultaneous credentials cannot be maintained, teams must coordinate planned maintenance or synchronized rolling updates with connection retry logic; universal zero-downtime rotation cannot be promised.
  - If an upstream operator revokes the old password before:
    1. ESO completes its reconciliation loop, AND
    2. All application pods complete a rolling restart, AND
    3. Downstream authentication is verified with the new credential,
  - **Result:** Every active pod in the cluster immediately enters an authentication failure storm (`FATAL: password authentication failed`), triggering cascading 500 errors and CrashLoopBackOff states.

---

## 4. The Six Diagnostic Golden Rules

1. **ExternalSecret `SecretSynced=True` does NOT prove the app is using the new password.** It only proves the Kubernetes Secret object in etcd has been updated.
2. **Kubernetes Secret `.data` updated does NOT prove the running container has seen it.** If delivered via environment variables, `/proc/$PID/environ` remains frozen to the boot-time value.
3. **Mounted volume file updated does NOT prove application RAM has reloaded it.** In-memory database pools continue authenticating with the old password until an internal reload or pod restart occurs.
4. **Secret mounted using `subPath` does NOT receive automated updates from kubelet.** Inodes bound via `subPath` never observe Secret mutations on disk; pods must be restarted.
5. **Successful application adoption in memory does NOT guarantee downstream authentication.** Downstream service permissions, network routes, and account locks must be verified independently.
6. **Where simultaneous valid credentials are supported, maintain a dual-credential overlap window until 100% of workload replicas complete a verified rolling rollout under the new credential before revoking the old credential.** For single-credential systems, plan coordinated rollout windows.
