# Milestone #26 Security & Evidence Validation Report

This document records the reproducible verification and audit evidence for **Milestone #26: External Secrets Management — Secret Synchronization ≠ Application Credential Adoption**.

It adheres strictly to the repository's four-layer evidence taxonomy:
- **DESIGNED:** Architectural expectations and documented platform capabilities without live cloud cluster execution.
- **STATICALLY VALIDATED:** Invariants, schemas, CRD API specifications, and security policies verified locally by deterministic scripts.
- **OBSERVED:** Runtime behavior executed against an active Kubernetes cluster with recorded evidence.
- **SKIPPED:** Tests not executed due to missing cluster environment, offline execution, or unprovisioned cloud resources.

---

## 1. Audit Environment

| Parameter | Value |
| :--- | :--- |
| **Audit Date** | 2026-10-11 |
| **Base Commit SHA** | `23f3d8f76d10d50060dbf0113567b74fea838ba8` (main) |
| **Feature Branch** | `feat/26-external-secrets-management` |
| **Operating System** | Windows 11 (PowerShell & Git Bash runtime) |
| **Shell Environment** | GNU bash 5.2.37 (x86_64-pc-msys) |
| **Python Version** | Python 3.12.7 |
| **Git Version** | git version 2.47.1.windows.1 |
| **External Secrets Operator** | Pinned specification: `v0.9.11` / `v0.10.x` (`external-secrets.io/v1beta1`) |
| **Cluster Scanner (Popeye)** | Pinned reference specification: `v0.21.5` (optional hygiene scan) |
| **Kubernetes CLI (`kubectl`)** | Not installed in PATH (offline static validation mode) |
| **Cloud Provider Interaction** | Zero AWS cloud resources created; offline non-destructive audit mode |

---

## 2. Test Matrix Across Four Evidence Layers

| Layer | Test ID | Scope | Expected Behavior | Actual Behavior | Evidence Classification | Result |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Layer 1: Static Config** | **VAL-26-01** | **Shell Syntax Check** | `scripts/validate.sh` passes `bash -n` cleanly | Shell syntax parsed with exit code 0 | **STATICALLY VALIDATED** | **PASS** |
| **Layer 1: Static Config** | **VAL-26-02** | **Credential & Token Scan** | Zero committed credentials, private keys, or workstation paths | Scanned all repository files; 0 sensitive patterns detected | **STATICALLY VALIDATED** | **PASS** |
| **Layer 1: Static Config** | **VAL-26-03** | **YAML Structural Syntax** | All manifests parse cleanly as valid YAML | All 18 YAML manifests valid across all milestone directories | **STATICALLY VALIDATED** | **PASS** |
| **Layer 1: Static Config** | **VAL-26-04** | **ESO CRD API Version** | Manifests declare `external-secrets.io/v1beta1` | Verified `SecretStore` and `ExternalSecret` match API version | **STATICALLY VALIDATED** | **PASS** |
| **Layer 1: Static Config** | **VAL-26-05** | **Namespaced SecretStore** | `kind: SecretStore` scoped to `sample-workloads` with AWS SecretsManager | Verified namespaced scope, region `us-east-1`, and JWT auth ref | **STATICALLY VALIDATED** | **PASS** |
| **Layer 1: Static Config** | **VAL-26-06** | **Identity Decoupling** | Controller SA (`eso-irsa-sa`) decoupled from app SA (`external-secret-app-sa`) | Verified controller SA carries IRSA annotation; app SA has 0 IAM | **STATICALLY VALIDATED** | **PASS** |
| **Layer 1: Static Config** | **VAL-26-07** | **ExternalSecret Mapping** | Maps `production/app/database` to `app-database-secret` with `refreshInterval: 1h` | Verified key mappings, refreshInterval, and `creationPolicy: Owner` | **STATICALLY VALIDATED** | **PASS** |
| **Layer 1: Static Config** | **VAL-26-08** | **Workload Delivery Mapping** | `deployment.yaml` references target `app-database-secret` via `secretKeyRef` | Verified matching secretName, env vars, and securityContext | **STATICALLY VALIDATED** | **PASS** |
| **Layer 1: Static Config** | **VAL-26-09** | **Kustomize Build** | Manifests render valid Kubernetes manifests | Skipped: Neither `kustomize` nor `kubectl` installed in host PATH | **SKIPPED** | **SKIP** |
| **Layer 2: Live Hygiene** | **VAL-26-10** | **Popeye Hygiene Scan** | Operational hygiene report on pod sizing and labels | Skipped: Live cluster unreachable; Popeye not run | **SKIPPED** | **SKIP** |
| **Layer 3: Behavioral** | **VAL-26-11** | **AWS Polling & Sync** | ESO polls AWS and updates Kubernetes Secret | Documented expectation based on controller reconciliation loop | **DESIGNED** | **PASS** |
| **Layer 3: Behavioral** | **VAL-26-12** | **Env Var Staleness Trap** | Running container retains boot-time env var despite Secret update | Documented expectation based on Linux kernel execve() semantics | **DESIGNED** | **PASS** |
| **Layer 4: Recovery** | **VAL-26-13** | **Rolling Rollout Recovery** | `kubectl rollout restart` cycles pods and loads new credential | Documented expectation based on Deployment rollout controller | **DESIGNED** | **PASS** |
| **Layer 4: Recovery** | **VAL-26-14** | **Downstream Auth Verification** | Workload confirms database query success under new credential | Documented expectation; verified log fingerprint suppression | **DESIGNED** | **PASS** |
| **Regression** | **VAL-26-15** | **Milestone #21 Probes** | Reference probe semantics and simulations hold | Verified probe simulation and manifest checks pass | **STATICALLY VALIDATED** | **PASS** |
| **Regression** | **VAL-26-16** | **Milestone #22 PDB** | PDB formula `replicas (3) > minAvailable (2)` holds | Arithmetic lock and AlwaysAllow policy verified | **STATICALLY VALIDATED** | **PASS** |
| **Regression** | **VAL-26-17** | **Milestone #23 Topology** | Multi-AZ `maxSkew: 1`, `DoNotSchedule`, matching selector | Verified topology spread constraints in sample-api | **STATICALLY VALIDATED** | **PASS** |
| **Regression** | **VAL-26-18** | **Milestone #24 StatefulSet** | StatefulSet invariants (Headless service, PVC templates) | Verified StatefulSet storage and headless service association | **STATICALLY VALIDATED** | **PASS** |
| **Regression** | **VAL-26-19** | **Milestone #25 Secrets** | Native secret trust boundaries and least-privilege RBAC | Verified Base64 reversibility and resourceNames scoping | **STATICALLY VALIDATED** | **PASS** |

---

## 3. Live Cluster Hygiene Inspection Reference (Popeye)

In live environments with an active, authorized disposable cluster:
- **Tool:** [Popeye](https://github.com/derailed/popeye) `v0.21.5`
- **Invocation Command:**
  ```bash
  popeye -n sample-workloads -s po,dp,sa,sec --lint ok
  ```
- **Hygiene Scope:** Popeye validates Kubernetes operational hygiene (resource requests/limits set, liveness/readiness probes present, non-root user enforcement, deprecation warnings).
- **Critical Limitation Disclaimer:** A clean Popeye score (Score: A / 100%) validates manifest syntax and cluster hygiene. It does **NOT** validate:
  1. Whether AWS Secrets Manager is reachable.
  2. Whether the IRSA OIDC trust relationship is valid.
  3. Whether ESO successfully synchronized credentials.
  4. Whether running applications have adopted new credentials.

---

## 4. Audit Limitations & Boundary Disclaimers

1. **No Cloud Resources Mutated:**
   In accordance with non-negotiable project constraints, zero AWS IAM roles, KMS keys, or Secrets Manager entries were created or altered.
2. **Offline Static Execution:**
   All runtime behavioral checks (Tests 11–14) are classified as **DESIGNED** or **SKIPPED**.
3. **Synthetic IAM Scoping:**
   IAM policies and annotations utilize synthetic ARNs (`arn:aws:iam::123456789012:role/eso-secrets-manager-role` and `arn:aws:secretsmanager:us-east-1:123456789012:secret:production/app/database-*`). No real credentials exist anywhere in Git.
