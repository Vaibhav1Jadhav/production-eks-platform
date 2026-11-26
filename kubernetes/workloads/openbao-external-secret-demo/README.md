# OpenBao External Secret Demo Workload

This directory contains the declarative Kubernetes manifests demonstrating enterprise secret management using the **External Secrets Operator (ESO)** and **OpenBao** as a self-hosted secret backend.

It models the central systems principle of Milestone #26:

$$
\text{OpenBao Sync} = \text{Kubernetes Secret Updated} \ne \text{Application Process Adopted} \ne \text{Downstream Auth Successful}
$$

---

## 1. Supported Component Versions & Architectural Rationale

| Component | Pinned Version / API | Purpose |
| :--- | :--- | :--- |
| **External Secrets Operator (ESO)** | `v0.9.11` | In-cluster controller reconciling cloud and self-hosted secrets. |
| **CRD API Version** | `external-secrets.io/v1beta1` | Supported specification for `SecretStore` and `ExternalSecret`. |
| **External Backend** | OpenBao (KV v2 engine) | Self-hosted, community-governed open-source secrets backend (Linux Foundation / MPL-2.0). |
| **Provider Schema Used** | `spec.provider.vault` | OpenBao preserves Vault v1/v2 HTTP API compatibility. ESO v0.9.11 contains no native `provider.openbao` CRD block; the Vault provider is the officially supported integration path. |
| **Authentication Method** | Kubernetes Auth (`auth.kubernetes`) | Projected ServiceAccount JWT exchanged with OpenBao for scoped client token. |
| **TLS Enforcement** | `caProvider` (ConfigMap) | Enforces TLS verification against OpenBao's internal CA trust anchor. |
| **Target Workload Namespace** | `sample-workloads` | Dedicated tenant namespace. |

---

## 2. Manifest Catalog

```text
kubernetes/workloads/openbao-external-secret-demo/
├── serviceaccount.yaml   # Decoupled identities: openbao-client-sa (Kubernetes auth) vs openbao-app-workload-sa
├── ca-configmap.yaml     # Synthetic OpenBao CA bundle for TLS verification
├── secretstore.yaml      # Namespaced SecretStore targeting OpenBao via Vault-compatible provider
├── externalsecret.yaml   # ExternalSecret mapping OpenBao 'secret/app/database' to openbao-database-secret
├── deployment.yaml       # Workload consuming secret via environment variables
├── kustomization.yaml    # Declarative resource bundle
└── README.md             # Workload documentation
```

### Identity Decoupling & Security Boundaries (`serviceaccount.yaml`)
- `openbao-client-sa`: The controller identity presented by ESO to OpenBao's `/v1/auth/kubernetes/login` endpoint. OpenBao validates the token with Kubernetes TokenReview and issues a short-lived token bound to role `openbao-app-role`.
- `openbao-app-workload-sa`: The application runtime identity. Possesses **zero OpenBao permissions** and **zero Kubernetes Secret API permissions**, preventing workloads from querying OpenBao or reading sibling secrets via the API.
- **Security Boundary Realities:**
  - Denying OpenBao access prevents lateral traversal to the secret backend, but the workload still receives the local Kubernetes Secret in its container.
  - Workload compromise exposes its own delivered credentials locally.
  - Identities with `pods/create` or `pods/exec` in the namespace can access secrets. Pod Security Standards (PSS) enforce execution security (non-root, read-only root fs), but do NOT restrict Secret references. Restricting Secret access requires namespace RBAC and fine-grained admission policies.

### Namespaced Provider Scope (`secretstore.yaml`)
- Configures `kind: SecretStore` targeting OpenBao KV v2 mount `secret`.
- Points to `https://openbao.security-system.svc.cluster.local:8200` with TLS verification via `openbao-ca-cert`.
- A namespaced `SecretStore` provides local namespace configuration scope, but does not independently guarantee complete tenant isolation without corresponding OpenBao ACL policies, Kubernetes RBAC, and admission control.

### Declarative Reconciliation (`externalsecret.yaml`)
- Defines `refreshInterval: 1h` to poll OpenBao for credential changes.
- Declares `creationPolicy: Owner`, ensuring the generated Kubernetes Secret `openbao-database-secret` is lifecycle-bound to the `ExternalSecret`.

### Workload Consumption & The Delivery Plane Staleness Trap (`deployment.yaml`)
- Consumes `openbao-database-secret` via `env[].valueFrom.secretKeyRef`.
- **The Delivery Trap:** When OpenBao rotates the credential and ESO mutates `openbao-database-secret`, `/proc/$$/environ` remains frozen to the process boot-time snapshot. The workload continues authenticating with the old credential until restarted via `kubectl rollout restart`.
- **The Three Delivery Mechanisms:**
  1. *Environment Variables:* Frozen at container `execve()` startup.
  2. *Projected Volumes:* Updated asynchronously via `..data` symlink swap, but requires in-memory app reload.
  3. *Mounted via `subPath`:* Bound to file inode at container creation; updates are **never** delivered automatically.

---

## 3. Safe Verification Walkthrough (Dry-Run / Non-Destructive)

### Client-Side Dry-Run Validation
Verify manifest composition without cluster modification:

```bash
kubectl apply --dry-run=client -k kubernetes/workloads/openbao-external-secret-demo/
```

### Inspection Commands (Safe, Zero-Credential Exposure)
```bash
# 1. Verify SecretStore readiness:
kubectl get secretstore openbao-store -n sample-workloads

# 2. Verify ExternalSecret synchronization condition:
kubectl get externalsecret openbao-database-credential-sync -n sample-workloads -o wide

# 3. Inspect synchronization events:
kubectl describe externalsecret openbao-database-credential-sync -n sample-workloads

# 4. Verify generated Secret metadata:
kubectl get secret openbao-database-secret -n sample-workloads -o jsonpath='{.metadata.resourceVersion}'
```
