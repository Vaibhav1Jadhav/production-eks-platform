# Controlled Lab: Verifying Stateful Pod Replacement & State Recovery

## Status: DESIGNED / PROCEDURE READY

> [!NOTE]
> **Evidence Status: DESIGNED / PROCEDURE READY**
> This lab provides a reproducible step-by-step verification procedure designed for disposable test clusters (KIND, Minikube, or non-production EKS). It uses synthetic test markers only. If a cluster with dynamic storage provisioning is not actively connected during automated testing, this procedure is classified as **SKIPPED** in accordance with repository evidence rules.

---

## Objective

Demonstrate that when an individual Pod belonging to a `StatefulSet` is deleted:
1. The StatefulSet controller recreates the Pod with the **identical ordinal name** (`stateful-demo-0`).
2. The replacement Pod reattaches to the **exact same PersistentVolumeClaim** (`data-stateful-demo-0`).
3. Historical data and synthetic state markers written before the deletion **survive and remain accessible**.
4. The application detects the state recovery, updates its recovery metrics, and successfully passes readiness checks.

---

## Safety & Pre-requisites

- **Safety Invariant:** Never execute destructive tests against production clusters or real databases. This lab uses a synthetic, non-critical test workload.
- **Cluster Requirement:** Kubernetes v1.25+ with a configured default `StorageClass` supporting dynamic volume provisioning (e.g., `standard` on KIND/minikube, `gp2` or `gp3` on EKS).
- **Tooling:** `kubectl`, `curl` (or `kubectl exec`).

---

## Step-by-Step Lab Procedure

### Step 1: Deploy the Stateful Workload

Apply the Kustomize overlay for `stateful-demo`:

```bash
kubectl apply -k kubernetes/workloads/stateful-demo/
```

Verify that the StatefulSet and Headless Service are created:

```bash
kubectl get statefulset,svc -n sample-workloads -l app.kubernetes.io/name=stateful-demo
```

---

### Step 2: Wait for Workload Readiness

Watch the sequential, ordered rollout of the replicas (`stateful-demo-0`, then `stateful-demo-1`):

```bash
kubectl rollout status statefulset/stateful-demo -n sample-workloads --timeout=120s
```

Verify that both pods achieve `1/1 Ready`:

```bash
kubectl get pods -n sample-workloads -l app.kubernetes.io/name=stateful-demo -o wide
```

---

### Step 3: Record Initial Identity, PVC, and Synthetic State

Capture the initial state of Pod 0:

```bash
# 1. Record Pod 0 Node and IP
kubectl get pod stateful-demo-0 -n sample-workloads -o jsonpath='{"Pod: "}{.metadata.name}{"\nNode: "}{.spec.nodeName}{"\nIP: "}{.status.podIP}{"\n"}'

# 2. Record bound PVC and PV volume name
kubectl get pod stateful-demo-0 -n sample-workloads -o jsonpath='{"Mounted Claim: "}{.spec.volumes[?(@.name=="data")].persistentVolumeClaim.claimName}{"\n"}'
ORIGINAL_PV=$(kubectl get pvc data-stateful-demo-0 -n sample-workloads -o jsonpath='{.spec.volumeName}')
echo "Original PV Volume: $ORIGINAL_PV"

# 3. Read initial identity marker
kubectl exec stateful-demo-0 -n sample-workloads -c stateful-demo -- cat /data/identity-marker.txt
```

*Expected Output Format:*
```text
ordinal_identity=stateful-demo-0
created_at=2026-10-05T17:00:00Z
restarts=0
last_boot=2026-10-05T17:00:00Z
```

---

### Step 4: Write a Synthetic Custom Marker

Write an additional synthetic test marker directly into the mounted persistent volume:

```bash
kubectl exec stateful-demo-0 -n sample-workloads -c stateful-demo -- \
  sh -c 'echo "portfolio-test-marker-$(date +%s)" > /data/synthetic-test-payload.txt'

# Verify the marker is written
kubectl exec stateful-demo-0 -n sample-workloads -c stateful-demo -- cat /data/synthetic-test-payload.txt
```

---

### Step 5: Terminate Pod 0

Simulate an abrupt Pod crash or node failure by forcibly deleting Pod 0:

```bash
kubectl delete pod stateful-demo-0 -n sample-workloads --now
```

---

### Step 6: Observe Replacement Reconciliation

Immediately watch the StatefulSet controller detect the missing ordinal and schedule the replacement:

```bash
kubectl get pods -n sample-workloads -l app.kubernetes.io/name=stateful-demo -w
```

Wait until replacement `stateful-demo-0` transitions back to `1/1 Ready`:

```bash
kubectl wait --for=condition=Ready pod/stateful-demo-0 -n sample-workloads --timeout=90s
```

---

### Step 7: Verify Identity, Storage Association, and Data Persistence

Confirm that the replacement Pod inherited the exact same identity, storage, and data:

```bash
# 1. Verify Ordinal Name
NEW_NAME=$(kubectl get pod stateful-demo-0 -n sample-workloads -o jsonpath='{.metadata.name}')
echo "Replacement Pod Name: $NEW_NAME"

# 2. Verify Bound PVC and PV
NEW_PV=$(kubectl get pvc data-stateful-demo-0 -n sample-workloads -o jsonpath='{.spec.volumeName}')
echo "Reassociated PV: $NEW_PV"

# 3. Assert Volume Continuity
if [ "$ORIGINAL_PV" = "$NEW_PV" ]; then
  echo "[PASS] PersistentVolume identity preserved: $ORIGINAL_PV"
else
  echo "[FAIL] PV mismatch! Storage was reprovisioned."
fi

# 4. Verify Application State Recovery
echo "=== Application Marker Content ==="
kubectl exec stateful-demo-0 -n sample-workloads -c stateful-demo -- cat /data/identity-marker.txt

# 5. Verify Synthetic Test Marker
echo "=== Synthetic Custom Marker ==="
kubectl exec stateful-demo-0 -n sample-workloads -c stateful-demo -- cat /data/synthetic-test-payload.txt
```

*Expected Verification Criteria:*
- Replacement Pod name is strictly `stateful-demo-0`.
- Bound PV name matches `$ORIGINAL_PV` exactly.
- `identity-marker.txt` reflects `restarts=1` and contains `recovered_at=...` with the original `created_at` intact.
- `synthetic-test-payload.txt` contains the exact string written before deletion.

---

### Step 8: Clean Up Lab Resources

```bash
kubectl delete -k kubernetes/workloads/stateful-demo/

# Explicitly clean up PVCs (retained by default for data safety)
kubectl delete pvc -l app.kubernetes.io/name=stateful-demo -n sample-workloads
```

---

## Operational Takeaway

Deleting a StatefulSet Pod destroys the compute container, but leaves the ordinal identity, the PersistentVolumeClaim, and the underlying cloud storage intact. When the StatefulSet reconciles, the replacement attaches to the same volume and resumes state, demonstrating that Pod destruction does not equal data destruction.
