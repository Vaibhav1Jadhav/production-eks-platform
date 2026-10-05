# Runbook: Stateful Workload Recovery & Storage Attachment Investigation

## Incident Overview

- **Severity:** High (Stateful Workload Degraded / Unhealthy Replica / Blocked Recovery)
- **Symptom:** A stateful workload replica for `stateful-demo` is missing, stuck in `Pending`, trapped in `ContainerCreating`, crashing, or failing readiness probes after a node disruption or pod replacement:
  ```text
  NAME              READY   STATUS              RESTARTS   AGE
  stateful-demo-0   0/1     ContainerCreating   0          9m
  stateful-demo-1   1/1     Running             0          42m
  ```
- **Target Audience:** Platform Engineers, SREs, and Kubernetes Cluster Operators.
- **Goal:** Systematically trace the stateful recovery diagnostic chain from the controller layer down to physical block storage, isolating whether the failure is an orchestration defect, scheduling constraint, PVC binding failure, CSI attachment deadlock, filesystem mount failure, application state corruption, or readiness probe gate.

---

## The Outside-In Diagnostic Chain

Never stop at *"The Pod is Running."* For a stateful workload, follow the strict evidence chain:

```text
Unhealthy / Missing Stateful Replica
        ↓
Question 1: Does the expected StatefulSet identity exist?
(kubectl get statefulset,pods -n sample-workloads)
        ↓
Question 2: Is the Pod scheduled onto an eligible node?
(kubectl describe pod <pod> -> Events: FailedScheduling)
        ↓
Question 3: Is the expected PersistentVolumeClaim present?
(kubectl get pvc -n sample-workloads)
        ↓
Question 4: Is the claim Bound to a PersistentVolume?
(kubectl describe pvc <pvc> -> Status: Bound vs Pending)
        ↓
Question 5: What StorageClass and VolumeBindingMode are involved?
(kubectl get storageclass,pv)
        ↓
Question 6: Did volume attachment and filesystem mounting succeed?
(kubectl describe pod -> FailedAttachVolume, FailedMount; kubectl get volumeattachment)
        ↓
Question 7: Did the application process recover its state?
(kubectl logs <pod> -> WAL replay, corrupt state markers, permission errors)
        ↓
Question 8: Did readiness probes succeed?
(kubectl describe pod -> Readiness probe failed)
```

---

## Diagnostic Investigation Hierarchy

### Question 1: Does the Expected StatefulSet Identity Exist?

Verify the desired vs. current replicas in the StatefulSet and check which ordinal is missing or degraded:

```bash
kubectl get statefulset stateful-demo -n sample-workloads
kubectl get pods -n sample-workloads -l app.kubernetes.io/name=stateful-demo -o wide
```

#### Diagnostic Evaluation:
- **Desired > Current:** The StatefulSet controller has not yet created the replacement pod.
  - *Root Cause:* Controller manager queue congestion, invalid admission webhook blocking pod creation, or resource quota exhaustion in the namespace.
  - *Action:* Inspect `kube-controller-manager` logs or check `kubectl get events -n sample-workloads --sort-by='.metadata.creationTimestamp'`.
- **Ordinal Pod Exists:** Note the pod phase (`Pending`, `ContainerCreating`, `Running`, `CrashLoopBackOff`) and proceed to Question 2.

---

### Question 2: Is the Pod Scheduled onto an Eligible Node?

Check whether `kube-scheduler` successfully assigned the pod to a worker node:

```bash
kubectl describe pod <pod-name> -n sample-workloads
```

Inspect the `Node:` field and look at the `Events:` section for `FailedScheduling`.

#### Diagnostic Evaluation:
- **`Node: <none>` with `Warning FailedScheduling`:**
  - *Case A (Compute Exhaustion):* `0/6 nodes are available: 6 Insufficient cpu / memory.`
  - *Case B (Topology Spread / Skew):* `nodes didn't match PodTopologySpread.`
  - *Case C (Volume Zone Topology Conflict):* `0/6 nodes are available: 3 node(s) had volume node affinity conflict, 3 node(s) in different zone.`
    - *Explanation:* The existing PersistentVolume is physically bound to Availability Zone `us-east-1a`, but no eligible nodes in `us-east-1a` have available capacity or tolerate taints.
    - *Resolution:* Add node capacity to the specific Availability Zone where the volume resides. **Do not** attempt to force-schedule the pod to another zone.

---

### Question 3: Is the Expected PersistentVolumeClaim Present?

Inspect the PVCs associated with the stateful workload:

```bash
kubectl get pvc -n sample-workloads -l app.kubernetes.io/name=stateful-demo
```

For pod `stateful-demo-0`, verify that PVC `data-stateful-demo-0` exists.

#### Diagnostic Evaluation:
- **PVC is Missing:**
  - *Root Cause:* The PVC was accidentally deleted manually, or an aggressive cleanup script ran.
  - *Consequence:* The replacement pod cannot find its claim and will fail to initialize.
  - *Resolution:* If deleted, the claim must be restored from volume snapshots or the StatefulSet must be recreated. Check `kubectl get pv` to determine if the underlying PV survived in `Released` status.
- **PVC Exists:** Proceed to Question 4.

---

### Question 4: Is the Claim Bound?

Inspect the binding status of the claim:

```bash
kubectl describe pvc <pvc-name> -n sample-workloads
```

#### Diagnostic Evaluation:
- **`Status: Pending`:** The claim has not bound to a PersistentVolume.
  - *Event: `WaitForFirstConsumer`*: Normal behavior if the Pod has not yet been scheduled to a node.
  - *Event: `ProvisioningFailed`*: The cloud storage provisioner (e.g., AWS EBS CSI) failed to create the volume. Common causes: IAM permission errors on the EBS CSI driver role, AWS KMS key access denied, or invalid volume size/type parameters.
- **`Status: Bound`:** The claim is bound to a PV (e.g., `pvc-7b91a0c4-...`). Proceed to Question 5.

---

### Question 5: What PV & StorageClass Are Involved?

Inspect the volume and its governing StorageClass configuration:

```bash
kubectl get pv <pv-name> -o yaml
kubectl get storageclass <storageclass-name> -o yaml
```

#### Diagnostic Evaluation:
- **`volumeBindingMode: Immediate` vs `WaitForFirstConsumer`:**
  - If `Immediate`, the volume was provisioned without considering where the pod would run, potentially creating a cross-zone scheduling deadlock.
- **`reclaimPolicy: Delete` vs `Retain`:**
  - Note the reclaim policy. If `Delete`, any future deletion of the PVC will immediately erase the cloud block device.
- **Node Affinity:**
  - Inspect `pv.spec.nodeAffinity.required.nodeSelectorTerms`. Note the `topology.kubernetes.io/zone` match expression.

---

### Question 6: Did Volume Attachment and Mounting Succeed?

If the pod is assigned to a node but stuck in `ContainerCreating`, inspect attachment events:

```bash
kubectl describe pod <pod-name> -n sample-workloads
kubectl get volumeattachment
```

#### Common Error Signatures:

#### 1. Multi-Attach Error (`FailedAttachVolume`)
```text
Warning  FailedAttachVolume  attachdetach-controller  Multi-Attach error for volume "pvc-..."
Volume is already exclusively attached to one node (ip-10-0-1-12) and can't be attached to another (ip-10-0-1-95)
```
- **Root Cause:** The previous host node crashed or became partitioned without unmounting the cloud block storage volume. AWS EBS enforces strict single-instance attachment for RWO volumes.
- **Resolution:**
  1. Verify whether the old node (`ip-10-0-1-12`) is truly dead in the AWS EC2 console.
  2. If the instance is terminated in EC2, force delete the stale Node object in Kubernetes:
     ```bash
     kubectl delete node ip-10-0-1-12.ec2.internal
     ```
  3. Deleting the dead Node object prompts the `attachdetach-controller` to immediately release the volume lock and proceed with attachment to the new node.

#### 2. Timeout Waiting for Mount (`FailedMount`)
```text
Warning  FailedMount  kubelet  Unable to attach or mount volumes: timed out waiting for the condition
```
- **Root Cause:** The volume attached at the cloud API layer, but the local worker node OS failed to format or mount the block device (e.g., corrupted filesystem, missing kernel filesystem module, or mount path permission denial).
- **Resolution:** Check `dmesg` and kubelet logs on the host node (`journalctl -u kubelet`).

---

### Question 7: Did the Application Recover Its State?

Once the container transitions to `Running`, inspect application container logs to verify state integrity:

```bash
kubectl logs <pod-name> -n sample-workloads -c stateful-demo
```

#### Diagnostic Evaluation:
- **Look for State Recovery Markers:**
  ```text
  === Booting Stateful Replica stateful-demo-0 ===
  State recovery: found existing persistent state for stateful-demo-0
  ordinal_identity=stateful-demo-0
  restarts=1
  ```
- **Error Signatures:**
  - `Permission denied /data/...`: The container non-root UID (10001) cannot write to `/data`. Check `spec.securityContext.fsGroup: 10001` in `statefulset.yaml`.
  - `Corrupt state marker / checksum mismatch`: Physical volume data was corrupted during abrupt power loss. Application must execute crash recovery or restore from backup.
  - `CrashLoopBackOff`: Application panicked during WAL replay. Do not repeatedly restart; inspect application crash dump.

---

### Question 8: Did Readiness Probes Succeed?

Verify that the readiness probe accurately gates client traffic until state is confirmed:

```bash
kubectl describe pod <pod-name> -n sample-workloads | grep -A 5 "Readiness:"
```

#### Diagnostic Evaluation:
- **Readiness Probe Failing:**
  ```text
  Warning  Unhealthy  kubelet  Readiness probe failed: HTTP probe failed with statuscode: 404
  ```
  - *Root Cause:* The container process is alive, but has not completed initializing `/data/identity-marker.txt` or the web server is not serving the mount directory.
  - *System Behavior:* The pod remains `0/1 Ready`. The headless service and CoreDNS route traffic only according to endpoint readiness policy.
  - *Resolution:* Allow application warm-up to finish. If failing continuously, test local response using `kubectl exec`:
    ```bash
    kubectl exec <pod-name> -n sample-workloads -c stateful-demo -- cat /data/identity-marker.txt
    ```

---

## Safe Remediation Guidelines

1. **Never delete a PVC during an active incident** unless you have independently verified that a complete backup exists and you explicitly intend to discard all data.
2. **Never change `reclaimPolicy: Delete` during live debugging**—this does not protect already provisioned volumes retroactively.
3. **Do not terminate replacement pods in `ContainerCreating` repeatedly.** Cloud volume detachment is a serialized operation with strict rate limits. Deleting the pod restarts the attachment timer, extending outage duration.
4. **Always verify zone topology before scaling.** Adding replicas to a StatefulSet with zone-bound storage requires confirming that candidate zones have available worker nodes.
