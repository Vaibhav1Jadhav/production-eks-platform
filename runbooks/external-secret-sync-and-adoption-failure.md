# Runbook: External Secret Synchronization & Application Adoption Failure

## Incident Overview

- **Severity:** High (Application Authentication Failure / In-Flight Credential Staleness / Pod CrashLoopBackOff)
- **Symptom:** Workload fails to connect to upstream databases, logs HTTP 500 / 401 errors, enters `CreateContainerConfigError`, or throws `FATAL: password authentication failed` following a routine credential rotation.
- **Target Audience:** SREs, DevSecOps Engineers, and Platform Operators.
- **Foundational Incident Principle:**
  $$
  \text{ExternalSecret Ready} \ne \text{Application Process Adopted} \ne \text{Downstream Auth Successful}
  $$
  Never assume that a synchronized Kubernetes Secret means running pods have adopted the new password.

---

## The Outside-In Diagnostic Hierarchy

```text
Application Database Authentication Failure / 5xx Spike
        ↓
Question 1: What is the credential version in AWS Secrets Manager?
(aws secretsmanager describe-secret --secret-id production/app/database)
        ↓
Question 2: Can the ESO controller authenticate via EKS IRSA?
(kubectl get sa eso-irsa-sa -n sample-workloads -o yaml -> check role-arn)
        ↓
Question 3: Is the namespaced SecretStore in a Ready state?
(kubectl get secretstore aws-secrets-manager-store -n sample-workloads)
        ↓
Question 4: What is the ExternalSecret reconciliation condition?
(kubectl get externalsecret app-database-credential-sync -n sample-workloads -o wide)
        ↓
Question 5: Did the target Kubernetes Secret update in etcd?
(kubectl get secret app-database-secret -n sample-workloads -o jsonpath='{.metadata.resourceVersion}')
        ↓
Question 6: How does the Pod consume the secret (Environment Variable vs. Volume Mount)?
(kubectl get deployment external-secret-demo -n sample-workloads -o yaml -> check env vs volumeMounts)
        ↓
Question 7: Has the running application process been restarted to adopt the new credential?
(Compare Pod startTime against Secret creationTimestamp / rollout restart status)
        ↓
Question 8: Does the database accept the new credential?
(Verify upstream database user status and dual-credential window)
```

---

## Step-by-Step Diagnostic Hierarchy

### Question 1: What is the Credential Version in AWS Secrets Manager?
Verify whether the credential was actually updated at the cloud provider:

```bash
# Safe check: inspect version stages without printing secret payloads
aws secretsmanager list-secret-version-ids \
  --secret-id production/app/database \
  --query 'Versions[?contains(VersionStages, `AWSCURRENT`)].VersionId' \
  --output text
```

*Hypothesis Evaluation:*
- **If `AWSCURRENT` version was updated recently:** Proceed to Question 2.
- **If version has not changed:** The rotation never occurred in AWS; escalate to the DBA or identity team.

---

### Question 2: Can the ESO Controller Authenticate via IRSA?
Verify that the controller ServiceAccount exists and carries the required annotation:

```bash
kubectl get sa eso-irsa-sa -n sample-workloads -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}'
```

*Hypothesis Evaluation:*
- **If annotation is missing or misspelled:** Kubelet does not inject the projected OIDC token. Update `serviceaccount.yaml` with the correct IAM Role ARN.
- **If present:** Inspect the ESO controller pod logs for AWS STS errors:
  ```bash
  kubectl logs -n external-secrets -l app.kubernetes.io/name=external-secrets --tail=50 | grep -iE 'error|denied|token'
  ```
- **If `AccessDeniedException` or `AssumeRoleWithWebIdentity` fails:** Proceed to Question 3.

---

### Question 3: Is the SecretStore in a Ready State?
Inspect the status conditions of the namespaced `SecretStore`:

```bash
kubectl describe secretstore aws-secrets-manager-store -n sample-workloads
```

*Hypothesis Evaluation:*
- **If Status is `Valid: False`:** The provider configuration is rejected. Check `spec.provider.aws.region` and `spec.provider.aws.auth.jwt.serviceAccountRef`.
- **If Status is `Valid: True`:** The connection to AWS Secrets Manager is authenticated. Proceed to Question 4.

---

### Question 4: What is the ExternalSecret Reconciliation Condition?
Evaluate the `ExternalSecret` synchronization status:

```bash
kubectl get externalsecret app-database-credential-sync -n sample-workloads
```

*Hypothesis Evaluation:*
- **If Status is `SecretSyncedError / False`:** The sync failed. Inspect events:
  ```bash
  kubectl describe externalsecret app-database-credential-sync -n sample-workloads | grep -A 10 "Events:"
  ```
  - `AccessDeniedException`: The IAM role lacks `secretsmanager:GetSecretValue` on the specific secret ARN, or KMS key policy blocks `kms:Decrypt`.
  - `ResourceNotFoundException`: The secret name `production/app/database` does not exist in the configured AWS region (`us-east-1`).
- **If Status is `SecretSynced / True`:** The controller successfully wrote the secret to Kubernetes. Proceed to Question 5.

---

### Question 5: Did the Target Kubernetes Secret Update in etcd?
Verify that the localized Kubernetes Secret object was mutated:

```bash
kubectl get secret app-database-secret -n sample-workloads -o jsonpath='{.metadata.resourceVersion}{"\t"}{.metadata.creationTimestamp}{"\n"}'
```

*Hypothesis Evaluation:*
- **If `resourceVersion` did not increment:** ESO has not yet reconciled the latest rotation (waiting on `refreshInterval`). Trigger immediate reconciliation:
  ```bash
  kubectl annotate externalsecret app-database-credential-sync -n sample-workloads force-sync=$(date +%s) --overwrite
  ```
- **If `resourceVersion` is updated:** Proceed to Question 6.

---

### Question 6: Delivery Mechanism: Env Var vs. Volume Mount?
Inspect the Deployment manifest to determine how credentials enter the pod:

```bash
kubectl get deployment external-secret-demo -n sample-workloads -o jsonpath='{.spec.template.spec.containers[*].env[*].valueFrom.secretKeyRef.name}'
```

*Hypothesis Evaluation:*
- **If consumed via `env[].valueFrom.secretKeyRef`:** **THE APPLICATION IS STALE.** In Linux, process environment tables are immutable after `execve()`. Running containers will **never** observe the new secret without a restart. Proceed to Question 7.
- **If consumed via `volumeMounts`:** The file on `tmpfs` has been updated by kubelet, but the application in-memory connection pool may still hold the old credential. Proceed to Question 7.

---

### Question 7: Has the Workload Restarted to Adopt the Credential?
Compare the Pod start time against the Secret update time:

```bash
POD_START=$(kubectl get pods -n sample-workloads -l app.kubernetes.io/name=external-secret-demo -o jsonpath='{.items[0].status.startTime}')
echo "Pod Start Time: $POD_START"
```

*Hypothesis Evaluation:*
- **If Pod started BEFORE Secret update:** The running container is holding the old credential in memory.
- **Remediation:** Trigger a rolling restart:
  ```bash
  kubectl rollout restart deployment/external-secret-demo -n sample-workloads
  ```
  Wait for rollout completion:
  ```bash
  kubectl rollout status deployment/external-secret-demo -n sample-workloads --timeout=60s
  ```
- **If Pod started AFTER Secret update:** The pod has booted with the new credential. Proceed to Question 8.

---

### Question 8: Does the Downstream Service Accept the Credential?
If the pod booted with the new credential but authentication still fails:
- **Hypothesis:** The upstream database has revoked the credential, the user account is locked, or there is an encoding/property mismatch (e.g., JSON key `password` vs `passwd`).
- **Remediation:** Test the credential from a secure bastion against the database. If necessary, revert the database password to the previous value to restore service.

---

## Common Root Causes, Corrective Actions & Operational Consequences

| Failure Domain | Observed Symptom | Root Cause | Corrective Action | Operational Consequence & Blast Radius |
| :--- | :--- | :--- | :--- | :--- |
| **IAM Authorization** | `AccessDeniedException` in ExternalSecret events | IAM role policy missing trailing wildcard on secret ARN (`database-*`). | Update IAM policy with trailing wildcard `*`. | **Low:** Policy change takes effect in seconds via STS; does not disrupt existing workloads. |
| **KMS Decrypt Lockout** | `AccessDeniedException: kms:Decrypt` | Customer Managed Key (CMK) key policy does not allow ESO IAM role. | Add `kms:Decrypt` permission to KMS CMK policy. | **Low:** Allows ESO to decrypt payload. Zero workload disruption. |
| **IRSA OIDC Mismatch** | `AssumeRoleWithWebIdentity failed` | IAM Role trust policy specifies wrong namespace or ServiceAccount. | Update OIDC trust policy `sub` condition to match `sample-workloads:eso-irsa-sa`. | **Low:** Restores STS token exchange. Zero workload disruption. |
| **Sync Interval Delay** | ExternalSecret shows old data for up to 1h | Default `refreshInterval: 1h` has not elapsed since cloud update. | Annotate ExternalSecret with `force-sync=$(date +%s)`. | **Low:** Forces immediate reconciliation loop without waiting for next poll. |
| **Environment Staleness** | DB auth failure after successful sync | Workload uses `env[].valueFrom.secretKeyRef`; Linux environment table is frozen. | Execute `kubectl rollout restart deployment/external-secret-demo`. | **Medium:** Causes rolling pod restart across all replicas. Ensure PDB permits disruption to avoid transient 5xx errors. |
| **In-Memory Pool Caching** | DB auth failure on volume mount | App read volume file at boot into static RAM pool; never re-reads disk. | Execute `kubectl rollout restart deployment/external-secret-demo`. | **Medium:** Cycles all pod replicas. Guarantees fresh connection pool initialization. |
| **Premature Revocation** | Immediate 100% auth failure across cluster | DB admin revoked old password before ESO synced or pods restarted. | Temporarily re-enable old password on DB cluster until rollout completes. | **High Emergency:** Restores production availability immediately; re-attempt rotation with a dual-credential overlap window. |
