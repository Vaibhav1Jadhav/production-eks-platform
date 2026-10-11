# External Secret Demo Workload: Synchronization & Adoption Boundary

This directory contains the declarative Kubernetes manifests demonstrating enterprise secret management using the **External Secrets Operator (ESO)** and **AWS Secrets Manager**.

It proves the foundational systems lesson:

$$
\text{Secret Synchronization} \ne \text{Application Credential Adoption}
$$

An external operator can successfully poll cloud state and update a native Kubernetes `kind: Secret` object, but a running application process will continue executing with stale, revoked credentials if delivered through process environment variables or cached in-memory connection pools.

---

## 1. Supported Component Versions & Prerequisites

| Component | Pinned Version / API | Purpose |
| :--- | :--- | :--- |
| **External Secrets Operator (ESO)** | `v0.9.11` / `v0.10.x` | In-cluster controller reconciling cloud secrets into Kubernetes. |
| **CRD API Version** | `external-secrets.io/v1beta1` | Stable specification for `SecretStore` and `ExternalSecret`. |
| **Cloud Provider** | AWS Secrets Manager (`us-east-1`) | Authoritative, KMS-encrypted secret repository. |
| **Authentication Mechanism** | AWS IAM Roles for Service Accounts (IRSA) | Web identity federation via EKS OIDC identity provider. |
| **Target Workload Namespace** | `sample-workloads` | Dedicated tenant namespace. |

---

## 2. Manifest Catalog

```text
kubernetes/workloads/external-secret-demo/
├── serviceaccount.yaml   # Decoupled identities: eso-irsa-sa (IRSA) vs external-secret-app-sa
├── secretstore.yaml      # Namespaced SecretStore targeting AWS Secrets Manager
├── externalsecret.yaml   # ExternalSecret mapping production/app/database to app-database-secret
├── deployment.yaml       # Workload consuming secret via environment variables
├── kustomization.yaml    # Declarative resource bundle
└── README.md             # Workload documentation
```

### Identity Decoupling (`serviceaccount.yaml`)
- `eso-irsa-sa`: Dedicated controller identity annotated with `eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/eso-secrets-manager-role"`. Used exclusively by the ESO controller to authenticate against AWS STS.
- `external-secret-app-sa`: Workload runtime identity. Possesses **zero AWS IAM permissions** and **zero Kubernetes API secret permissions**. Workloads never interact with AWS APIs directly.

### Namespaced Provider Scope (`secretstore.yaml`)
- Declares a namespaced `kind: SecretStore` rather than a global `ClusterSecretStore`.
- Scopes AWS provider credentials to `sample-workloads`, preventing lateral access from other namespaces.

### Declarative Reconciliation (`externalsecret.yaml`)
- Defines `refreshInterval: 1h` to poll AWS Secrets Manager for credential updates.
- Defines `spec.target.creationPolicy: Owner`, ensuring the generated Kubernetes Secret is lifecycle-bound to the `ExternalSecret`.
- Extracts individual keys (`username`, `password`) from the JSON payload at `production/app/database`.

### Workload Consumption & The Staleness Trap (`deployment.yaml`)
- Consumes `app-database-secret` via `env[].valueFrom.secretKeyRef`.
- Runs as an unprivileged user (`runAsUser: 10001`, `readOnlyRootFilesystem: true`, `capabilities.drop: ["ALL"]`).
- Logs only password length and SHA256 cryptographic fingerprint prefixes (`sha256sum | cut -c1-8`), strictly avoiding credential exposure in container logs.
- **The Failure Trap:** When AWS Secrets Manager rotates the credential and ESO updates `app-database-secret`, `/proc/$$/environ` remains frozen to the startup value. The workload continues attempting downstream authentication with the old, revoked credential until restarted via `kubectl rollout restart`.

---

## 3. Safe Verification Walkthrough (Disposable Cluster / Dry-Run)

### Client-Side Dry-Run Validation
Verify manifest composition without modifying cluster state:

```bash
kubectl apply --dry-run=client -k kubernetes/workloads/external-secret-demo/
```

### Inspection Commands (Safe, Zero-Credential Exposure)
Verify reconciliation state without printing sensitive payloads:

```bash
# 1. Verify SecretStore readiness:
kubectl get secretstore -n sample-workloads

# 2. Verify ExternalSecret synchronization condition:
kubectl get externalsecret app-database-credential-sync -n sample-workloads -o wide

# 3. Inspect synchronization events:
kubectl describe externalsecret app-database-credential-sync -n sample-workloads

# 4. Verify generated Secret metadata (timestamps and resourceVersion):
kubectl get secret app-database-secret -n sample-workloads -o jsonpath='{.metadata.resourceVersion}'
```
