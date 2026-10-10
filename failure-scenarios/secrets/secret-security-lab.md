# Hands-On Lab: Secret Security & Trust-Boundary Verification

## Status: DESIGNED / PROCEDURE READY

> [!NOTE]
> **Evidence Status: DESIGNED / PROCEDURE READY**
> This lab provides a reproducible step-by-step verification procedure designed for execution on a disposable local cluster (such as kind, minikube, or a sandbox EKS cluster). Manifests are statically validated in `scripts/validate.sh`. Command outputs below represent expected test behavior and are labeled as **ILLUSTRATIVE**.

---

## 1. Laboratory Objective

Verify the Kubernetes-native secret trust boundaries under controlled conditions:
1. **Base64 Reversibility:** Prove that Base64 encoding provides representation rather than confidentiality.
2. **Safe Manifest Generation:** Generate a Secret out-of-band using `--dry-run=client -o yaml` without committing the output to Git.
3. **Least-Privilege RBAC:** Verify that `secret-demo-sa` can read `demo-app-secret` via `get`, but cannot `list` secrets or access unauthorized objects.
4. **Volume Mount Delivery:** Verify in-memory `tmpfs` volume projection with `0400` read-only permissions.
5. **Log Hygiene:** Verify that the workload logs contain metadata and checksum prefixes without leaking credentials.

---

## 2. Part 1: The Base64 Representation Boundary

Test the mathematical reversibility of Base64 encoding locally:

```bash
# Synthetic test credential
RAW_SECRET="DevOpsSecret#2026!"

# 1. Encode into Base64 (Standard Kubernetes representation)
ENCODED=$(echo -n "$RAW_SECRET" | base64)
echo "Encoded representation: $ENCODED"

# 2. Decode from Base64 back to plaintext (Zero keys required)
DECODED=$(echo -n "$ENCODED" | base64 --decode)
echo "Decoded plaintext:      $DECODED"
```

*Illustrative Output:*
```text
Encoded representation: RGV2T3BzU2VjcmV0IzIwMjYh
Decoded plaintext:      DevOpsSecret#2026!
```

**System Conclusion:** Base64 is an invertible representation function $f(x)$ with inverse $f^{-1}(x)$. It provides zero confidentiality. Anyone who can read the Base64 string can recover the plaintext with standard shell utilities.

---

## 3. Part 2: Safe Out-of-Band Secret Creation

Do not commit raw secrets to Git. Instead, provision the Secret imperatively in the cluster namespace:

```bash
# 1. Ensure the namespace exists
kubectl create namespace sample-workloads --dry-run=client -o yaml | kubectl apply -f -

# 2. Create the secret imperatively out-of-band
kubectl create secret generic demo-app-secret \
  --namespace=sample-workloads \
  --from-literal=password='DevOpsSecret#2026!' \  # Never commit real credentials to Git
  --dry-run=client -o yaml | kubectl apply -f -
```

*Illustrative Output:*
```text
secret/demo-app-secret created
```

> [!WARNING]
> While `--dry-run=client -o yaml` avoids persistent cluster modification, piping it to a file produces a manifest with unencrypted Base64 data. Do **NOT** redirect this command's output into repository files.

---

## 4. Part 3: Deploy the Workload & RBAC

Apply the declarative workloads, ServiceAccount, and least-privilege RBAC policies:

```bash
kubectl apply -f kubernetes/workloads/secret-demo/serviceaccount.yaml
kubectl apply -f kubernetes/workloads/secret-demo/role.yaml
kubectl apply -f kubernetes/workloads/secret-demo/rolebinding.yaml
kubectl apply -f kubernetes/workloads/secret-demo/deployment.yaml
```

Wait for the deployment to become ready:

```bash
kubectl rollout status deployment/secret-demo -n sample-workloads --timeout=60s
```

*Illustrative Output:*
```text
deployment "secret-demo" successfully rolled out
```

---

## 5. Part 4: Verify RBAC Authorization Boundaries

Use `kubectl auth can-i` to evaluate authorization decisions:

### Test 4A: Authorized Single-Object Retrieval
```bash
kubectl auth can-i get secret/demo-app-secret \
  --as=system:serviceaccount:sample-workloads:secret-demo-sa \
  --namespace=sample-workloads
```
*Expected Output:*
```text
yes
```

### Test 4B: Unauthorized Collection Listing
```bash
kubectl auth can-i list secrets \
  --as=system:serviceaccount:sample-workloads:secret-demo-sa \
  --namespace=sample-workloads
```
*Expected Output:*
```text
no
```

### Test 4C: Unauthorized Access to Unrelated Secret
```bash
kubectl auth can-i get secret/unrelated-private-key \
  --as=system:serviceaccount:sample-workloads:secret-demo-sa \
  --namespace=sample-workloads
```
*Expected Output:*
```text
no
```

---

## 6. Part 5: Verify Volume Mount & File Permissions

Inspect the mounted volume inside the running container:

```bash
# 1. Identify a running pod
POD_NAME=$(kubectl get pods -n sample-workloads -l app.kubernetes.io/name=secret-demo -o jsonpath='{.items[0].metadata.name}')

# 2. Verify file presence and permissions (mode 0400 = -r--------)
kubectl exec -n sample-workloads "$POD_NAME" -- ls -l /etc/secrets/demo-app-secret/password
```

*Expected Output:*
```text
-r--------    1 root     root            18 Oct  8 00:25 /etc/secrets/demo-app-secret/password
```

Verify that the volume is backed by an in-memory `tmpfs` mount rather than persistent node disk:

```bash
kubectl exec -n sample-workloads "$POD_NAME" -- df -T /etc/secrets/demo-app-secret
```

*Expected Output:*
```text
Filesystem           Type       1K-blocks      Used Available Use% Mounted on
tmpfs                tmpfs        1048576         4   1048572   0% /etc/secrets/demo-app-secret
```

---

## 7. Part 6: Verify Application Log Hygiene

Verify that the container process does NOT leak the password into stdout/stderr logs:

```bash
kubectl logs -n sample-workloads "$POD_NAME" --tail=10
```

*Expected Output:*
```text
secret-consumer process initialized. Awaiting secret volume delivery...
Secret volume active. Mode: -r--------, Size: 18B, Fingerprint: 0a69a239..., Health: OK
```

The log entry confirms:
- File permissions are active (`-r--------`).
- File size is tracked (`18B`).
- A SHA256 cryptographic fingerprint prefix is recorded (`0a69a239...`).
- The plaintext password (`DevOpsSecret#2026!`) is **never printed**.

---

## 8. Teardown & Safe Cleanup

Remove all test resources:

```bash
kubectl delete -f kubernetes/workloads/secret-demo/deployment.yaml
kubectl delete -f kubernetes/workloads/secret-demo/rolebinding.yaml
kubectl delete -f kubernetes/workloads/secret-demo/role.yaml
kubectl delete -f kubernetes/workloads/secret-demo/serviceaccount.yaml
kubectl delete secret demo-app-secret -n sample-workloads
```

Verify complete removal:

```bash
kubectl get secret demo-app-secret -n sample-workloads
```

*Expected Output:*
```text
Error from server (NotFound): secrets "demo-app-secret" not found
```
