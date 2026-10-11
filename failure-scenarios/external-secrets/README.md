# External Secrets Failure Scenarios: Synchronization vs. Adoption

When a platform integrates an external secret manager like AWS Secrets Manager, operations teams frequently conflate **secret synchronization** with **application credential adoption**.

```text
ExternalSecret Ready ≠ Application Credential Adopted ≠ Downstream Auth Successful
```

A healthy controller status asserts only that the external payload was successfully written into the Kubernetes API server (`etcd`). It does not verify that running application processes have reloaded their memory or that downstream services accept the new credential.

---

## The Four System Failure Boundaries

```text
                             ┌────────────────────────────────────────────────────────┐
                             │       External Secret Lifecycle Failure Boundaries     │
                             └───────────────────────────┬────────────────────────────┘
                                                         │
             ┌───────────────────────────┬───────────────┴───────────────┬───────────────────────────┐
             ▼                           ▼                               ▼                           ▼
      ┌─────────────┐             ┌─────────────┐                 ┌─────────────┐             ┌─────────────┐
      │ Boundary 1  │             │ Boundary 2  │                 │ Boundary 3  │             │ Boundary 4  │
      │ Provider IAM│             │ Controller  │                 │ Application │             │ Downstream  │
      │  & Network  │             │ Sync Freeze │                 │ Memory Stale│             │ Revocation  │
      └──────┬──────┘             └──────┬──────┘                 └──────┬──────┘             └──────┬──────┘
             │                           │                               │                           │
      IAM denied,                 ESO crashes or                  Kubernetes    │             Database drops
      KMS key locked,             refreshInterval                 Secret updated,             old password  
      STS trust fails.            polling delay.                  app holds old               before pods   
             │                           │                        env variable.               cycle.        
             ▼                           ▼                               ▼                           ▼
      ExternalSecret              Kubernetes                      Process keeps               Immediate     
      Ready=False                 Secret is stale                 using old key               cluster-wide  
      (SecretSyncedError)         in etcd                         (Auth failures)             outage storm  
```

---

## Scenario Index

| Document | Focus Area | Core Insight | Evidence Status |
| :--- | :--- | :--- | :--- |
| **[Scenario 1: Synchronization Succeeded, Application Stayed Stale](sync-success-app-stale.md)** | Workload Delivery & Memory Staleness | ExternalSecret reports `SecretSynced=True` and Kubernetes Secret is updated, but running containers retain boot-time environment variables indefinitely until restarted. | **DESIGNED / STATICALLY VALIDATED** |
| **[Scenario 2: Provider Access Denied (IAM & KMS)](provider-access-denied.md)** | EKS IRSA & Cloud Authorization | ExternalSecret fails with `AccessDeniedException` due to OIDC trust mismatches, KMS key policy restrictions, or missing secret ARN permissions. | **DESIGNED / STATICALLY VALIDATED** |
| **[Scenario 3: Premature Old-Credential Revocation](premature-revocation.md)** | Upstream Lifecycle & Transition Windows | Revoking old credentials at the database before ESO syncs and before pods restart causes an immediate, catastrophic authentication outage across all replicas. | **DESIGNED / STATICALLY VALIDATED** |
| **[Validation Evidence: External Secrets Audit Matrix](validation.md)** | 4-Layer Verification & Evidence Matrix | Comprehensive test matrix covering static YAML/CRD parsing, IAM least-privilege scoping, Popeye hygiene notes, and recovery validation. | **STATICALLY VALIDATED / AUDITED** |

---

## Evidence Classification Reference

All scenarios in this directory adhere strictly to the repository's evidence standards:

- **DESIGNED:** Expected architectural behavior derived from Kubernetes kernel semantics, process environment immutability, and ESO controller reconciliation specifications.
- **STATICALLY VALIDATED:** Configuration syntax, CRD schema compatibility (`external-secrets.io/v1beta1`), IRSA role annotations, and delivery references verified by `scripts/validate.sh`.
- **ILLUSTRATIVE:** Representative commands, API responses, and event logs demonstrating diagnostic workflows without fabricating live cluster execution.
- **OBSERVED:** Runtime verification captured from an active Kubernetes cluster with verifiable output.
- **SKIPPED:** Real-world runtime execution was skipped when no live cluster was connected.
