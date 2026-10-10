# Kubernetes Secret Failure Scenarios: Trust Boundaries & Lifecycle Failures

A credential enters a Kubernetes cluster. Wrapping it in a `kind: Secret` object with Base64 encoding changes its representation, not its confidentiality.

The security of a Kubernetes Secret depends entirely on the trust boundaries established across its lifecycle:
- Who can query the API server to retrieve it?
- Does authorization isolate individual secrets or expose the whole namespace?
- How does the credential enter the container filesystem without lingering on physical disks?
- What stops the application from leaking it into centralized logging?
- What happens when the credential changes in the API while the application is still running?

```text
Base64 Encoding ≠ Confidentiality
Secret API Object ≠ Secure Storage
Authorized Delivery ≠ Safe Application Handling
```

---

## The Four Secret Failure Boundaries

```text
                        ┌─────────────────────────────────────────────────────────┐
                        │             Secret Lifecycle & Failure Domains          │
                        └────────────────────────────┬────────────────────────────┘
                                                     │
        ┌───────────────────────┬────────────────────┴──────────────────┬───────────────────────┐
        ▼                       ▼                                       ▼                       ▼
 ┌───────────────┐      ┌───────────────┐                       ┌───────────────┐       ┌───────────────┐
 │  Boundary 1   │      │  Boundary 2   │                       │  Boundary 3   │       │  Boundary 4   │
 │ Unauthorized  │      │ Over-Permitted│                       │ Runtime Leak  │       │ Stale In-Mem  │
 │  API Access   │      │ Blast Radius  │                       │ Downstream    │       │   Rotation    │
 └───────┬───────┘      └───────┬───────┘                       └───────┬───────┘       └───────┬───────┘
         │                      │                                       │                       │
 Identity lacks         Identity receives                       Secret delivered        Secret updated  
 RBAC permission        broad list/watch                        cleanly via tmpfs,      in API/volume,  
 to read Secret.        across namespace.                       application logs        application     
         │                      │                               credential.             caches old key. 
 Result: 403            Result: Attacker                                │                       │
 Forbidden error        dumps all tokens                        Result: Exfiltrated     Result: Auth    
 blocks workload        and credentials.                        into SIEM/APM.          failure storms. 
```

---

## Scenario Index

| Document | Focus Area | Core Insight | Status |
| :--- | :--- | :--- | :--- |
| **[Scenario 1: Unauthorized Secret Access](unauthorized-secret-access.md)** | RBAC Authorization & ServiceAccount Scoping | An identity attempts to fetch a Secret without explicit permissions. Diagnoses API 403 Forbidden errors via `kubectl auth can-i`. | **DESIGNED / STATICALLY VALIDATED** |
| **[Scenario 2: Over-Permissive Secret Access](over-permissive-secret-access.md)** | Blast Radius & `resourceNames` Isolation | Broad `list` and `watch` permissions expose every credential in the namespace. Scoping with `resourceNames` and `get` restricts retrieval. | **DESIGNED / STATICALLY VALIDATED** |
| **[Scenario 3: Secret Leakage Outside Kubernetes](secret-leakage-outside-kubernetes.md)** | Application Runtime & Telemetry Boundary | Kubernetes securely delivers the secret, but the application runtime leaks it via stdout/stderr, crash dumps, or debug endpoints. | **DESIGNED / STATICALLY VALIDATED** |
| **[Scenario 4: Secret Rotation Lifecycle Boundary](secret-rotation-lifecycle-boundary.md)** | In-Memory Staleness & Symlink Updates | Updating a Secret in Kubernetes updates the mounted file on `tmpfs`, but running application processes retain stale credentials in memory. | **DESIGNED / STATICALLY VALIDATED** |
| **[Hands-On Lab: Secret Security & Trust-Boundary Verification](secret-security-lab.md)** | Controlled Verification Procedure | Reproducible step-by-step lab demonstrating Base64 reversibility, out-of-band secret creation, volume delivery, and RBAC denial. | **DESIGNED / PROCEDURE READY** |
| **[Validation Evidence: Security Audit Matrix](validation.md)** | Reproducible Audit & Evidence Matrix | Complete test matrix, tool environment, static validation results, and boundary limitations for Milestone #25. | **STATICALLY VALIDATED / AUDITED** |

---

## Evidence Classification Reference

All scenarios in this directory adhere strictly to the repository's evidence standards:

- **DESIGNED:** Expected architectural behavior derived from Kubernetes API semantics, RBAC evaluation engine, and kubelet volume projection.
- **STATICALLY VALIDATED:** Configuration syntax, RBAC rules, ServiceAccount bindings, and volume mounts verified locally by `scripts/validate.sh`.
- **ILLUSTRATIVE:** Representative commands, API responses, and event logs demonstrating diagnostic workflows without fabricating live cluster execution.
- **OBSERVED:** Runtime verification captured from an active Kubernetes cluster with verifiable output.
- **SKIPPED:** Real-world runtime execution was skipped when no live cluster was connected.
