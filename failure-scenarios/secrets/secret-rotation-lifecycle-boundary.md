# Failure Scenario 4: Secret Rotation Lifecycle Boundary

## Status: DESIGNED / STATICALLY VALIDATED

> [!NOTE]
> **Evidence Status: DESIGNED / STATICALLY VALIDATED**
> The contrast between filesystem projection updates and in-memory process caching is statically verified in `scripts/validate.sh`. Command outputs below demonstrate standard kubelet volume rotation semantics and are labeled as **ILLUSTRATIVE**.

---

## 1. Situation

A production database password `demo-app-secret` is scheduled for routine quarterly rotation.

An engineer or automated pipeline updates the Secret object in the Kubernetes API with the new credential:

```bash
kubectl create secret generic demo-app-secret \
  --namespace=sample-workloads \
  --from-literal=password=<NEW_ROTATED_SECRET_VALUE> \
  --dry-run=client -o yaml | kubectl apply -f -
```

The database administrator revokes the old password on the database server.

---

## 2. Assumption

The platform operator observes:

> *"The Secret object was successfully updated in Kubernetes. Kubelet automatically updates mounted secret volumes. The application is now using the new password."*

The assumption is that when Kubernetes updates the Secret object, the running application process immediately and automatically begins authenticating with the new credential.

---

## 3. Hidden Problem

Within minutes of revoking the old password, the database server triggers hundreds of authentication rejections, and the application begins failing all client requests:

```bash
kubectl logs -n sample-workloads -l app.kubernetes.io/name=secret-demo --tail=10
```

*Illustrative Output:*
```text
[ERROR] Database connection failed: FATAL: password authentication failed for user "app_admin"
[ERROR] Retrying connection in 5s...
[ERROR] Database connection failed: FATAL: password authentication failed for user "app_admin"
```

Yet, if an operator execs into the container and checks the mounted secret file on disk:

```bash
kubectl exec -it -n sample-workloads deploy/secret-demo -- cat /etc/secrets/demo-app-secret/password
```

*Illustrative Output:*
```text
<NEW_ROTATED_SECRET_VALUE>
```

The file on disk **has** been updated to the new password! Why is the application failing?

---

## 4. Evidence: Filesystem vs. Memory Desynchronization

To understand why the application fails despite the file being updated, we inspect the container filesystem projection:

```bash
kubectl exec -it -n sample-workloads deploy/secret-demo -- ls -la /etc/secrets/demo-app-secret
```

*Illustrative Output:*
```text
total 0
drwxrwxrwt 3 root root 100 Oct  8 00:20 .
drwxr-xr-x 3 root root  40 Oct  8 00:15 ..
drwxr-xr-x 2 root root  60 Oct  8 00:20 ..2026_10_08_00_20_14.839129481
lrwxrwxrwx 1 root root  31 Oct  8 00:20 ..data -> ..2026_10_08_00_20_14.839129481
lrwxrwxrwx 1 root root  15 Oct  8 00:15 password -> ..data/password
```

Kubelet executed an atomic symlink swap:
1. Created new directory `..2026_10_08_00_20_14.839129481`.
2. Wrote the new password into `..2026_10_08_00_20_14.839129481/password`.
3. Updated the symlink `..data` to point to the new directory.

**The Failure:** The application process read the file once into an in-memory connection pool configuration when the container booted at `00:15`. It never registered an `inotify` watcher on `/etc/secrets/demo-app-secret/password`. The long-lived database connection pool in memory continues attempting to reconnect using the old, revoked password cached in the application's RAM.

---

## 5. Mechanism: The Secret Rotation Chain

```mermaid
flowchart TD
    subgraph K8S_ROTATION["1. Kubernetes Control Plane & Node Sync"]
        direction TB
        UpdateReq["Secret Object Updated\n(kubectl apply / API)"]
        KubeletSync["Kubelet Periodic Sync\n(~60-90s sync loop)"]
        AtomicSwap["Atomic Symlink Swap\n(..data -> new_timestamp_dir)"]
        DiskFile["Mounted File Updated\n/etc/secrets/.../password"]

        UpdateReq --> KubeletSync --> AtomicSwap --> DiskFile
    end

    subgraph APP_LIFECYCLE["2. Application Process Runtime"]
        direction TB
        ProcessBoot["Process Startup (00:15)\n(Reads file into memory)"]
        InMemVar["In-Memory Variable / Pool\n(db_password = OLD_CRED)"]
        LiveTraffic["Client Database Queries\n(Authenticates with OLD_CRED)"]
        
        ProcessBoot --> InMemVar --> LiveTraffic
    end

    subgraph OUTCOME["3. Desynchronization Boundary"]
        direction TB
        DBRevoke["Database Revokes OLD_CRED"]
        DiskFile -.->|"File has NEW_CRED\n(Process NEVER re-reads)"| InMemVar
        InMemVar -->|"Authenticates with OLD_CRED"| DBRevoke
        DBRevoke --> Outage["CRITICAL: Auth Failure Storm\n(Pods CrashLoop / 5xx)"]
    end

    classDef k8s fill:#1e293b,stroke:#3b82f6,stroke-width:2px,color:#f8fafc;
    classDef app fill:#1e293b,stroke:#f59e0b,stroke-width:2px,color:#f8fafc;
    classDef err fill:#450a0a,stroke:#ef4444,stroke-width:2px,color:#fca5a5;

    class UpdateReq,KubeletSync,AtomicSwap,DiskFile k8s;
    class ProcessBoot,InMemVar,LiveTraffic app;
    class DBRevoke,Outage err;
```

### Rotation Behavior Across Delivery Models

| Delivery Model | Filesystem Update | Process Memory Update | Required Operator Action |
| :--- | :--- | :--- | :--- |
| **Volume Mount (Directory Projection)** | Automatic (Kubelet sync loop, usually 60–90 seconds via atomic symlink swap of `..data`). | **Stale.** Process does not reload unless application implements `inotify` / file watching. | Restart pods via `kubectl rollout restart deployment/<name>` or implement in-process reloader. |
| **Volume Mount via `subPath`** | **None.** `subPath` directly bind-mounts the file inode; it is exempt from kubelet symlink updates. | **Stale.** File on disk never changes; process never sees updated credential. | Mandatory pod restart via `kubectl rollout restart deployment/<name>`. |
| **Environment Variable (`secretKeyRef`)** | **None.** Container environment is immutable after `execve()`. | **Stale.** Process cannot observe updated environment variable. | Mandatory pod restart via `kubectl rollout restart deployment/<name>`. |
| **Immutable Secret (`immutable: true`)** | **Prohibited.** Secret cannot be modified in place. | **N/A.** | Create `secret-v2`, update Deployment manifest to point to `secret-v2`, trigger rolling rollout. |

---

### The 7-Stage End-to-End Secret Rotation Lifecycle

Safe credential rotation requires decoupling the lifecycle into seven distinct, verifiable stages:

```mermaid
flowchart LR
    S1["1. Source Mutation\n(Change upstream DB/API)"] --> S2["2. K8s Secret Update\n(Apply to kube-apiserver)"]
    S2 --> S3["3. Workload Delivery\n(Kubelet tmpfs projection)"]
    S3 --> S4["4. Application Reload\n(Inotify / Pod restart)"]
    S4 --> S5["5. App Authenticates\n(Verify live connection)"]
    S5 --> S6["6. Upstream Revocation\n(Revoke old credential)"]
    S6 --> S7["7. Recovery / Rollback\n(Fallback if auth breaks)"]
```

1. **Stage 1 — Authoritative Source Mutation:** The upstream service (e.g., database, third-party API) provisions the new credential while temporarily accepting both old and new credentials (dual-credential transition window).
2. **Stage 2 — Kubernetes Secret Object Update:** The Secret object in the Kubernetes control plane is updated (`kubectl apply` or secret synchronization).
3. **Stage 3 — Workload Filesystem Delivery:** The kubelet receives the update and refreshes the projected directory volume via atomic symlink swap (or replacement pod startup). *(Note: `subPath` mounts will NOT update in place).*
4. **Stage 4 — Application Detection & In-Memory Reload:** The application detects the new file via filesystem watcher (`inotify`) or by restarting the container process (`kubectl rollout restart`).
5. **Stage 5 — Workload Downstream Authentication Verification:** The running application successfully authenticates against the upstream service using the new credential.
6. **Stage 6 — Authoritative Source Old Credential Revocation:** Only after 100% of workload replicas are verified healthy under the new credential is the old credential deactivated at the authoritative source.
7. **Stage 7 — Rollback & Recovery Window:** If authentication fails at Stage 5, the cluster can safely revert to the previous credential before Stage 6 occurs.

### Rotation vs. Revocation: Crucial Distinction
- **Rotation** is the process of generating, distributing, and adopting a new credential across all consumers.
- **Revocation** is the destruction or invalidation of the old credential at the authoritative source.
- **The Golden Rule:** Never revoke the old credential until all consumers have demonstrably switched and verified authentication with the new one.

### Dangerous Operational Assumptions
1. **Assumption:** *"Updating the Kubernetes Secret updates all running applications."*
   **Reality:** Running processes cache credentials in heap memory; without an inotify watcher or pod rollout, the process never reads the new value.
2. **Assumption:** *"A successful volume refresh guarantees successful authentication."*
   **Reality:** The filesystem contains bytes; it does not validate that those bytes are syntactically valid or accepted by the upstream database.
3. **Assumption:** *"A restarted Pod proves the old credential was revoked."*
   **Reality:** Pod restarts ensure the new Secret is read, but do not affect the upstream database. If the old credential is never revoked, it remains an active attack surface.
4. **Assumption:** *"A successful Secret sync proves the external credential is valid."*
   **Reality:** Kubernetes accepts arbitrary Base64 strings. It performs zero validation against external authorization servers.

---

## 6. Resolution: Safe Rotation Strategies

To avoid the in-memory staleness trap, platform teams adopt one of three patterns:

### Strategy 1: Controlled Rolling Restart (Recommended Baseline)
Whenever a Secret is updated, trigger a rolling deployment rollout:

```bash
kubectl rollout restart deployment/secret-demo -n sample-workloads
```

This guarantees:
1. Replacement pods mount the updated secret directory at startup.
2. The readiness probe verifies connectivity before the old replica is terminated.
3. Zero in-memory staleness occurs.

### Strategy 2: In-Process Reloaders / File Watchers
For services that cannot tolerate restarts:
- Use an `inotify` file watcher inside the application process that detects changes to `/etc/secrets/demo-app-secret/..data`.
- Trigger an internal connection pool refresh without terminating the HTTP server.

### Strategy 3: Versioned Immutable Secrets
- Deploy `demo-app-secret-v2`.
- Update `deployment.yaml` `volumes[0].secret.secretName: demo-app-secret-v2`.
- GitOps / CI triggers a declarative rollout.

---

## 7. Trade-off & Engineering Judgment

### The Trade-off
- **Relying on Kubelet Automatic Volume Updates:** Avoids restarting pods, but risks silent desynchronization where the filesystem has the new credential while application RAM holds the old one.
- **Enforcing Deployment Rollouts on Rotation:** Incurs the compute overhead of cycling pods and re-evaluating readiness probes, but provides deterministic, verifiable guarantees that every running process is using the new credential.

### Engineering Judgment
Updating a Secret in Kubernetes only updates the storage layer; it does not update running application memory. In production, never assume a credential rotation is complete until every consuming Pod replica has been verified against the new credential.
