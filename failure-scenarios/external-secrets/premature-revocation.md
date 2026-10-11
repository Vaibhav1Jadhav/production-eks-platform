# Failure Scenario 3: Premature Old-Credential Revocation

## Status: DESIGNED / STATICALLY VALIDATED

> [!NOTE]
> **Evidence Status: DESIGNED / STATICALLY VALIDATED**
> Rotation lifecycle boundaries and dual-credential transition policies are verified statically in `scripts/validate.sh`. Command outputs below demonstrate database connection failures and rolling update timelines, labeled as **ILLUSTRATIVE**.

---

## 1. Situation

A high-throughput payment processing service in `sample-workloads` queries an Amazon Aurora PostgreSQL database.

Security compliance mandates monthly database credential rotation.

At `10:00 AM`, the database administrator:
1. Generates a new database password `Pass#2026_v2` in AWS Secrets Manager under `production/app/database`.
2. Changes the master database password on the PostgreSQL cluster to `Pass#2026_v2`.
3. **Immediately drops/revokes the previous password `Pass#2026_v1`** to ensure old credentials cannot be used.

---

## 2. Assumption

The database administrator observes:

> *"The new password is live in AWS Secrets Manager. Kubernetes uses the External Secrets Operator to manage secrets. The platform will automatically handle credential propagation."*

The assumption is that external secret synchronization happens synchronously and instantaneously across all running application instances.

---

## 3. Hidden Problem

Within seconds of dropping the old password, 100% of the application pods begin logging authentication errors:

```bash
kubectl logs -n sample-workloads -l app.kubernetes.io/name=external-secret-demo --tail=10
```

*Illustrative Output:*
```text
2026-10-11T10:00:15Z [FATAL] org.postgresql.util.PSQLException: FATAL: password authentication failed for user "app_admin"
2026-10-11T10:00:17Z [FATAL] Connection pool exhausted. Terminating worker threads.
2026-10-11T10:00:20Z [ERROR] HTTP 500 Internal Server Error returned to client traffic.
```

Client error rates spike from 0.01% to 100%. The service experiences a complete platform outage.

---

## 4. Evidence (Timeline Breakdown)

To understand why the outage occurred, we evaluate the timeline discrepancy across the three system layers:

```text
Timeline of the Outage:

10:00:00 AM ── Database Admin updates AWS Secrets Manager (v1 -> v2)
10:00:05 AM ── Database Admin REVOKES v1 on PostgreSQL server (CRITICAL MISTAKE)
10:00:10 AM ── Running Pods attempt database queries with v1 -> REJECTED (Outage Begins)
10:00:45 AM ── ESO is still waiting for its refreshInterval timer (Next poll at 10:45 AM)
10:01:00 AM ── Kubernetes Secret 'app-database-secret' STILL CONTAINS v1 IN ETCD!
10:05:00 AM ── Operator manually triggers rollout restart -> Replacement pods boot with v1!
10:05:30 AM ── Replacement pods STILL FAIL because etcd has not synced v2 yet!
```

**The Two Synchronization Gaps:**
1. **The Polling Window Gap:** `ExternalSecret` reconciles on a periodic schedule (`refreshInterval: 1h`). AWS Secrets Manager does not push events into Kubernetes by default; ESO pulls them. Between `10:00 AM` and `10:45 AM`, the Kubernetes Secret object in etcd **still holds the old password**.
2. **The Workload Rollout Gap:** Even after ESO updates the Kubernetes Secret, rolling out replacement pods across hundreds of replicas takes several minutes to satisfy readiness probes and PodDisruptionBudgets.

Because the old credential was revoked at `10:00:05 AM`, there was zero overlap window. The application had zero opportunity to transition gracefully.

---

## 5. Mechanism: The Dual-Credential Overlap Window

```mermaid
sequenceDiagram
    autonumber
    actor DBA as Database Admin
    participant DB as PostgreSQL Database
    participant SM as AWS Secrets Manager
    participant ESO as External Secrets Operator
    participant K8s as Kubernetes Secret (etcd)
    participant Pods as Application Pods

    DBA->>DB: 1. Configure DB to accept BOTH v1 and v2 (Dual-Credential Window)
    DBA->>SM: 2. Update Secret to v2 in AWS Secrets Manager
    Note over ESO,K8s: Polling interval elapses
    ESO->>SM: 3. Poll and detect v2
    ESO->>K8s: 4. Reconcile and update Secret in etcd
    DBA->>Pods: 5. Trigger kubectl rollout restart
    Pods->>K8s: 6. Replacement pods boot, read v2, pass readiness probes
    Pods->>DB: 7. 100% of replicas now authenticate using v2
    DBA->>DB: 8. ONLY NOW: Revoke v1 on database server (Safe Revocation)
```

### The Golden Rule of Credential Lifecycle
$$
\text{Rotation} \ne \text{Revocation}
$$
- **Rotation:** Generating and propagating a new credential.
- **Revocation:** Invalidating the previous credential.
- **Invariant:** Revocation must always lag rotation by the maximum propagation and verification window (typically 24–48 hours in production).

---

## 6. Resolution & Prevention

### Prevention: Dual-Credential Policy
For stateful databases supporting multiple active user passwords (e.g., PostgreSQL, MySQL, MongoDB, AWS IAM):
1. **Provision New Password:** Add the new password hash to the user record alongside the existing password.
2. **Synchronize Cloud:** Update AWS Secrets Manager.
3. **Verify ESO Synchronization:** Confirm `ExternalSecret` reports `SecretSynced` and check that `app-database-secret` contains the new checksum.
4. **Execute Rolling Rollout:** Run `kubectl rollout restart deployment/external-secret-demo -n sample-workloads`.
5. **Verify Telemetry:** Monitor database authentication logs for 24 hours to confirm zero queries are utilizing `v1`.
6. **Revoke Old Password:** Decommission `v1` at the database.

### Emergency Recovery: If Premature Revocation Occurs
If an upstream credential is inadvertently dropped before workloads transition:
1. **Immediate Database Rollback:** Re-enable the previous password on the database server immediately to restore service to running pods.
2. **Do Not Churn Pods:** Restarting pods while the upstream database rejects credentials simply converts running pods into `CrashLoopBackOff` pods.
