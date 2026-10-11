# Runbook: Secret Access, Mounting & Delivery Failure Investigation

## Incident Overview

- **Severity:** High (Application Authentication Failure / Container Startup Blocker / Pod CrashLoopBackOff)
- **Symptom:** Workload fails to start, crashes immediately with `CreateContainerConfigError`, enters `CrashLoopBackOff`, fails readiness probes, reports HTTP 403 Forbidden when requesting the Kubernetes API, or logs authentication failures against downstream databases:
  ```text
  NAME                           READY   STATUS                       RESTARTS   AGE
  secret-demo-789cf94cb-9x2pk   0/1     CreateContainerConfigError   0          2m
  ```
- **Target Audience:** Platform Engineers, SREs, and Kubernetes Cluster Operators.
- **Goal:** Systematically trace the secret delivery pipeline from identity and API authorization down to node volume mount projection, container filesystem permissions, and application in-memory consumption.

---

## The Outside-In Diagnostic Hierarchy

Never assume a secret issue is simply a missing password. Distinguish clearly:

$$
\text{Symptom} \ne \text{Failure Domain} \ne \text{Root Cause}
$$

Follow the strict evidence chain:

```text
Application Credential Failure / CrashLoopBackOff
        ↓
Question 1: Which ServiceAccount identity is the workload using?
(kubectl get pod <pod> -n sample-workloads -o jsonpath='{.spec.serviceAccountName}')
        ↓
Question 2: Does the target Secret object exist in the namespace?
(kubectl get secret <secret-name> -n sample-workloads)
        ↓
Question 3: Access Mode Branch: Is delivery via Volume Mount or Direct API Query?
├─ If Direct In-Pod API: Does the ServiceAccount have RBAC authorization?
│  (kubectl auth can-i get secret/<secret-name> --as=... -n sample-workloads)
└─ If Volume Mount / Env Var: Kubelet handles retrieval. Was Pod admission rejected?
   (kubectl describe pod <pod> -> Check admission webhook rejections)
        ↓
Question 4: Did the kubelet successfully mount the Secret volume on tmpfs?
(kubectl describe pod <pod> -> Events: FailedMount, MountVolume.SetUp failed)
        ↓
Question 5: Can the non-root container process read the mounted file permissions?
(ls -l /etc/secrets/... -> defaultMode: 0400 vs container UID/GID / fsGroup)
        ↓
Question 6: Did the Secret rotate, leaving the application process with a stale in-memory key?
(File timestamp vs process start time vs database auth errors)
        ↓
Question 7: Is the upstream authoritative credential valid at the target service?
(Verify whether the password works directly against the target database/API)
```

---

## Step-by-Step Diagnostic Hierarchy

### Question 1: Which ServiceAccount Identity is the Workload Using?

Determine the identity assigned to the failing pod:

```bash
kubectl get pod -n sample-workloads -l app.kubernetes.io/name=secret-demo \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\tServiceAccount: "}{.spec.serviceAccountName}{"\n"}{end}'
```

*Hypothesis Evaluation:*
- **If `default` or unexpected SA:** The Deployment manifest failed to specify `serviceAccountName: secret-demo-sa`. The Pod is running with ambient, unprivileged credentials.
- **If `secret-demo-sa`:** Proceed to Question 2.

---

### Question 2: Does the Target Secret Exist in the Namespace?

Verify whether the referenced Secret object is present in the pod's namespace:

```bash
kubectl get secret demo-app-secret -n sample-workloads
```

*Hypothesis Evaluation:*
- **If `Error from server (NotFound)`:** The Secret was never created, was created in the wrong namespace, or was deleted by an administrative script. Kubelet cannot start containers that reference non-existent secret volumes unless `optional: true` is explicitly configured.
- **If present:** Note the age and type (`Opaque`), then proceed to Question 3.

---

### Question 3: Access Mode Branch: Volume Mount vs. Direct API Query

> [!IMPORTANT]
> **Dual-Track Access Boundary:**
> - **Volume Projection (`volumes[].secret`) & Env Vars (`secretKeyRef`):** The **kubelet daemon** retrieves the Secret using worker node credentials and Node authorization. The workload's ServiceAccount does **NOT** require `get secret` RBAC permissions. If the workload uses volume projection, skip to Question 4.
> - **Direct In-Pod API Queries:** If the application process queries `kube-apiserver` via in-cluster token (`GET /api/v1/namespaces/.../secrets/...`), RBAC is enforced against the ServiceAccount.

If evaluating in-pod API queries, evaluate the RBAC authorization boundary using `kubectl auth can-i`:

```bash
kubectl auth can-i get secret/demo-app-secret \
  --as=system:serviceaccount:sample-workloads:secret-demo-sa \
  --namespace=sample-workloads
```

*Hypothesis Evaluation:*
- **If `no` (for in-pod API client):** RBAC authorization is missing. Inspect `Role` and `RoleBinding` in the namespace:
  ```bash
  kubectl get rolebindings -n sample-workloads -o wide
  kubectl describe role secret-reader-role -n sample-workloads
  ```
  Check whether `resourceNames` matches the target secret and `verbs` includes `get`.
- **If `yes` (or workload consumes via volume projection):** Proceed to Question 4.

---

### Question 4: Did Kubelet Mount the Secret Volume on `tmpfs`?

Inspect Pod lifecycle events for volume mounting errors:

```bash
kubectl describe pod -n sample-workloads -l app.kubernetes.io/name=secret-demo | grep -A 10 "Events:"
```

*Common Error Signatures:*
- `MountVolume.SetUp failed for volume "secret-volume" : secret "demo-app-secret" not found`: The volume definition in `deployment.yaml` references an incorrect secret name.
- `ContainerCreating (prolonged)`: Kubelet is waiting for API server synchronization of the secret object.

*Hypothesis Evaluation:*
- **If mount errors are present:** Fix the `volumes[].secret.secretName` reference in the Deployment manifest.
- **If mounts succeeded:** Proceed to Question 5.

---

### Question 5: Can the Container Process Read the Mounted File?

Check the POSIX file permissions and ownership of the mounted secret:

```bash
POD_NAME=$(kubectl get pods -n sample-workloads -l app.kubernetes.io/name=secret-demo -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n sample-workloads "$POD_NAME" -- ls -ld /etc/secrets/demo-app-secret /etc/secrets/demo-app-secret/*
```

*Hypothesis Evaluation:*
- **If permissions are `0400` (`-r--------`) but owned by `root:root`:** If the container runs as a non-root user (`runAsUser: 10001`) and the filesystem is read-only, the non-root process may receive `Permission denied` when attempting to read a file owned by root with mode `0400`.
- **Resolution:** Configure `fsGroup: 10001` in `pod.spec.securityContext` or set `defaultMode: 0440` (mode 288 in decimal) so the container group can read the file.
- **If readable:** Proceed to Question 6.

---

### Question 6: Did the Secret Rotate While the Process Holds Stale Memory?

Compare the Secret object modification timestamp with the Pod start time:

```bash
SECRET_MTIME=$(kubectl get secret demo-app-secret -n sample-workloads -o jsonpath='{.metadata.creationTimestamp}')
POD_START=$(kubectl get pod -n sample-workloads "$POD_NAME" -o jsonpath='{.status.startTime}')

echo "Secret Creation: $SECRET_MTIME"
echo "Pod Start Time:  $POD_START"
```

Inspect the container filesystem symlink:

```bash
kubectl exec -n sample-workloads "$POD_NAME" -- ls -la /etc/secrets/demo-app-secret
```

*Hypothesis Evaluation:*
- **If Secret was modified after Pod Start:** The filesystem projection was updated by kubelet (unless mounted with `subPath`), but the application process loaded the password into memory at startup. The in-memory connection pool is using an expired or revoked credential.
- **Recovery:** Trigger a rolling restart:
  ```bash
  kubectl rollout restart deployment/secret-demo -n sample-workloads
  ```
- **If Secret was not rotated or already rolled out:** Proceed to Question 7.

---

### Question 7: Is the Upstream Authoritative Credential Valid?

Verify whether the credential payload currently stored in the Secret actually authenticates successfully against the upstream service (e.g., database, external API):

- **Hypothesis Evaluation:** The Secret object and volume mounts may be completely operational in Kubernetes, but the upstream database administrator may have expired the user account, locked the credentials, changed network firewalls, or applied a typo during out-of-band secret creation.
- **Diagnostic Action:** Test authentication directly against the database from a secure diagnostic bastion using the synthetic test credentials. Never print credentials in logs or CI output.

---

## Common Root Causes, Corrective Actions & Operational Consequences

| Failure Domain | Observed Symptom | Root Cause | Corrective Action | Operational Consequence & Blast Radius |
| :--- | :--- | :--- | :--- | :--- |
| **Namespace Boundary** | `Secret "demo-app-secret" not found` | Secret created in `default` instead of `sample-workloads`. | Re-create Secret in `sample-workloads`. | **Low to Medium:** Recreating an existing Secret disruptions any workloads that depend on previous keys; ensure consumer pods are coordinated. |
| **RBAC Verb Isolation** | `403 Forbidden` on API query | Role grants `list` with `resourceNames` (unsupported by k8s authz engine). | Change verb in Role to `get`. | **High Security Impact:** Never grant wildcard `*` or collection `list`/`watch` to resolve 403 errors; doing so broadens namespace blast radius and risks credential exfiltration. |
| **Volume Configuration** | `CreateContainerConfigError` / `MountVolume.SetUp failed` | Misspelled `secretName` in `deployment.spec.template.spec.volumes`. | Correct `volumes[].secret.secretName`. | **Medium:** Editing Deployment triggers a rolling update; monitor replica surge and ensure sufficient cluster capacity. |
| **Process Permissions** | `Permission denied: /etc/secrets/...` | Mode `0400` owned by root, container running as UID 10001. | Add `fsGroup: 10001` or set `defaultMode: 288` (0440). | **Medium:** Modifying `securityContext` triggers Pod termination and replacement. On large persistent volumes, recursive `fsGroup` changes can introduce startup latency. |
| **Rotation Staleness** | DB auth failure after rotation | Application cached credential in RAM at startup. | Run `kubectl rollout restart deployment/secret-demo`. | **Medium:** Causes rolling pod restart across all replicas. Ensure PDB permits disruption to avoid transient 5xx errors or capacity dips. |
| **SubPath Non-Updating** | Filesystem never updates after secret change | Secret key mounted via `volumeMounts[].subPath`. | Remove `subPath` in favor of directory projection, or mandate rolling restart on change. | **Medium:** Modifying volume mount structure requires redeployment. Existing container file paths may change. |
| **Upstream Revocation Failure** | Persistent auth failures on healthy pods | Upstream database revoked old password before pods reloaded. | Roll back upstream revocation or accelerate rolling restart across all consumers. | **High:** Workload authentication downtime until new credentials propagate to 100% of replicas. |
