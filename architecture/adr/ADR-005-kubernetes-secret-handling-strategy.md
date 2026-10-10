# ADR-005: Kubernetes Secret Handling & Trust-Boundary Strategy

## Status

Accepted

---

## Context

A container requires a database credential, API token, or TLS private key to perform its workload.

In a naive deployment, an operator stores credentials in a Kubernetes Secret manifest, encodes them using Base64, and commits the YAML file directly to a Git repository:

```yaml
# ILLUSTRATIVE / INSECURE EXAMPLE — NEVER COMMIT CREDENTIALS TO GIT
apiVersion: v1
kind: Secret
metadata:
  name: app-secret
data:
  password: <base64-encoded-secret>
```

This pattern conflates **data serialization** with **confidentiality**.

Base64 is a binary-to-text encoding scheme (RFC 4648). It translates arbitrary byte sequences into an ASCII string representation so that binary payloads safely pass through YAML parsers and HTTP JSON transports. It provides zero encryption, zero access control, and zero confidentiality. Any identity with read access to the manifest or Git commit history can recover the original plaintext in constant time ($O(1)$) using standard shell utilities without possessing any cryptographic key.

Managing credentials inside a platform requires modeling the entire lifecycle across five distinct system boundaries:

```text
Milestone #24 (Stateful Workloads)
Question: "Can the workload recover with the correct identity and state?"
└── Couples compute lifecycle to stable network identity and persistent storage.

            ↓

Milestone #25 (Kubernetes Secrets)
Question: "Who can retrieve the credential, where does it live, and where can it leak?"
└── Establishes Kubernetes-native trust boundaries across storage, API, delivery, and runtime.

            ↓

Milestone #26 (External Secrets & Key Management)
Question: "Where is the single source of truth, and how are secrets synchronized and rotated?"
└── Integrates external secret providers (AWS Secrets Manager, Vault, ESO).
```

---

## Problem Statement

A pervasive misconception in cloud-native operations is:

$$
\text{Base64 Encoding} = \text{Secret Management}
$$

Or equally dangerous:

$$
\text{Kubernetes Secret API Object} = \text{Encrypted, Leak-Proof Storage}
$$

A `kind: Secret` object in Kubernetes does not automatically encrypt data at rest, does not restrict who can view it unless RBAC is explicitly configured, does not prevent workloads from leaking values into log streams, and does not automatically update in-memory application variables upon rotation.

To build a resilient platform, we must determine:
1. **Source of Truth:** How do secrets enter the cluster without living in Git?
2. **Storage Confidentiality:** Where does the Secret reside at rest, and who can access the backing datastore?
3. **Retrieval Authorization:** How do we grant workloads access to their own secrets without exposing neighboring credentials?
4. **Workload Delivery:** Should credentials be delivered via filesystem volume projection or process environment variables?
5. **Runtime Boundary:** How do we prevent downstream application leaks into logs, crash dumps, and telemetry?
6. **Rotation Lifecycle:** What happens when a credential changes in the API versus what the application process holds in memory?

---

## Decision

We establish the following architectural policies for Kubernetes-native secret management:

### 1. Reject Committed Secret Payloads
- Secret manifests containing real or recoverable credential values must never be committed to Git.
- Deployable repository manifests use explicit synthetic placeholders (e.g., `<DEMO_SECRET_VALUE>`).
- For local verification and controlled labs, secrets are generated imperatively out-of-band:
  ```bash
  kubectl create secret generic demo-app-secret \
    --namespace=sample-workloads \
    --from-literal=password=<DEMO_SECRET_VALUE> \
    --dry-run=client -o yaml | kubectl apply -f -
  ```
- Documentation explicitly clarifies that `--dry-run=client -o yaml` still outputs unencrypted Base64 text and must not be redirected into committed repository files.

### 2. Standardize on Secret Volume Mounts over Environment Variables
Workloads must consume secrets primarily via volume mounts backed by in-memory `tmpfs` filesystems:
- Volumes are mounted with `readOnly: true` and `defaultMode: 0400` (mode 256 in decimal, restricting read permissions exclusively to the container process user).
- Files reside strictly in node RAM (`tmpfs`), preventing plaintext persistence onto worker node physical block storage.
- Environment variables (`valueFrom.secretKeyRef`) are rejected for long-lived sensitive credentials due to broad leakage vectors (process table `/proc/$PID/environ`, crash dumps, and child process inheritance).

### 3. Enforce Least-Privilege RBAC with `resourceNames` and Verb Isolation
- ServiceAccounts assigned to workloads must not receive wildcard `*` or broad namespace-wide `list` / `watch` permissions on `secrets`.
- Workload Roles must explicitly restrict access using `resourceNames: ["<target-secret>"]` and limit verbs strictly to `["get"]`.
- The architecture documents the API limitation that `resourceNames` cannot filter `list` or `watch` requests; granting `list` on secrets invariably exposes the entire namespace inventory.

### 4. Evaluate Immutable Secrets (`immutable: true`)
For versioned credentials, secrets should declare `immutable: true`:
- Closes API server watch loops on the kubelet, reducing control plane memory and network overhead.
- Protects running workloads against accidental in-place mutations.
- Enforces a predictable rolling update lifecycle: deploy `secret-v2`, update workload deployment spec, trigger rolling rollout, and prune `secret-v1`.

### 5. Intentional Deferral of External Secret Providers to Milestone #26
External Secret Management (External Secrets Operator, HashiCorp Vault, AWS Secrets Manager, and Secrets Store CSI Driver) is **explicitly deferred to Milestone #26**. 
Milestone #25 intentionally isolates the Kubernetes-native secret security model first, establishing the baseline boundaries and failure modes before introducing external synchronization controllers.

---

## Architectural Trust Boundaries

```text
[ Developer / CI Pipeline ]
         │ (1. Source Boundary: Git commits vs out-of-band generation)
         ▼
[ kube-apiserver ] ─── (2. Authorization Boundary: RBAC get vs list/watch)
         │
         ├─── (3. Storage Boundary: etcd plain protobuf vs KMS envelope encryption)
         ▼
[ kubelet Node Daemon ]
         │ (4. Delivery Boundary: tmpfs volume mount vs process environment)
         ▼
[ Container / Application ]
         │ (5. Runtime Boundary: in-memory variables vs stdout/stderr logs)
         ▼
[ Centralized Logging / SIEM ]
```

### Boundary 1: Storage at Rest (etcd vs. KMS)
- In default Kubernetes, secrets are written to etcd in unencrypted Base64 Protocol Buffer format.
- Encryption at rest requires configuring KMS envelope encryption on the EKS control plane (encrypting Data Encryption Keys with a Key Encryption Key in AWS KMS).
- *Classification:* **DESIGNED / DOCUMENTED** in this milestone, as AWS KMS infrastructure is not provisioned at the workload layer.

### Boundary 2: API Retrieval (RBAC)
- Workload identities (ServiceAccounts) must be constrained to their designated credentials.
- A workload that only requires `demo-app-secret` must not possess `list` permissions, which would permit enumerating TLS certificates, cluster tokens, or other application credentials in the same namespace.

### Boundary 3: Delivery Mechanism (Volume vs. Env)
- In-memory `tmpfs` volume mounts isolate credentials to dedicated filesystem paths with strict POSIX permissions (`0400`).
- Process environment variables leak across process trees and diagnostic tooling.

### Boundary 4: Application Runtime Handling
- Kubernetes RBAC and volume security cannot protect against poor application code.
- Applications that dump configuration dictionaries during startup, log HTTP request parameters containing credentials, or output unsanitized stack traces during database connection failures violate the runtime boundary.

### Boundary 5: Rotation Lifecycle
- Updating a Secret object in the API updates the mounted volume file on disk asynchronously (via kubelet symlink swap).
- However, if the application caches the credential in process heap memory at startup, the running application remains bound to the expired credential until the Pod is restarted.

---

## Alternatives Considered

### Alternative 1: Committed Secret Manifests in Git
- **Description:** Storing Base64-encoded `kind: Secret` manifests directly in Git alongside Deployment manifests.
- **Evaluation:** **REJECTED.** Base64 is reversible with zero keys. Committing encoded secrets to Git exposes credentials to every developer, CI runner, clone, and backup snapshot indefinitely.

### Alternative 2: Environment Variable Delivery (`valueFrom.secretKeyRef`)
- **Description:** Injecting secrets directly into the container process environment.
- **Evaluation:** **REJECTED as primary strategy.** While syntactically simple in Pod YAML, environment variables suffer from critical operational defects:
  1. Visible in `/proc/$PID/environ` to any process running as the same UID.
  2. Inherited automatically by all spawned child processes.
  3. Captured by standard APM, core dump, and crash reporting agents (e.g., Sentry, Datadog) upon unhandled exceptions.
  4. Immutable for the lifetime of the process (`execve`); rotating the Secret does not update the running container.

### Alternative 3: Broad Namespace RBAC (`verbs: ["get", "list", "watch"]`)
- **Description:** Allowing the workload ServiceAccount to list all secrets in its namespace.
- **Evaluation:** **REJECTED.** Expands blast radius. If the container process is compromised via remote code execution (RCE), the attacker can query the Kubernetes API to exfiltrate all secrets across the namespace.

### Alternative 4: Premature Adoption of External Secret Stores (Vault / ESO / AWS Secrets Manager)
- **Description:** Immediately installing External Secrets Operator or Secrets Store CSI Driver in Milestone #25.
- **Evaluation:** **DEFERRED to Milestone #26.** Introducing external controllers before demonstrating the Kubernetes-native trust model obscures foundational concepts (RBAC scoping, volume projection vs env vars, etcd storage boundaries, and application log leakage). Milestone #25 establishes the native baseline; Milestone #26 builds the enterprise synchronization pipeline upon it.

---

## Trade-offs & Consequences

### What We Gain
1. **Zero Secret Leakage in Git:** Clear separation between declarative infrastructure manifests and sensitive runtime credentials.
2. **Granular Blast Radius:** If a workload container is compromised, its ServiceAccount token can only read its designated Secret object (`resourceNames: ["demo-app-secret"]`), not neighboring secrets.
3. **Memory-Only Delivery:** Credentials reside in node RAM (`tmpfs`) with `0400` permissions, avoiding persistent disk writes.
4. **Predictable Lifecycle:** Clean delineation between API updates, filesystem symlink swaps, and application process reloads.

### What We Incur
1. **Operational Rigor for Lab Verification:** Deploying the demo requires an out-of-band creation step or pipeline injection rather than simple `kubectl apply -f .`.
2. **RBAC Limitation with `list`:** Workloads cannot dynamically discover their secrets via `kubectl get secrets` when constrained by `resourceNames`; applications must explicitly know their secret name.
3. **Application Responsibility for Rotation:** Applications consuming volume-mounted secrets must either implement in-process file watchers (inotify) to detect credential updates or rely on rolling pod restarts.
