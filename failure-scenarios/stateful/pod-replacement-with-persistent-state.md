# Failure Scenario 1: Pod Replacement with Persistent State

## Status: DESIGNED / STATICALLY VALIDATED

> [!NOTE]
> **Evidence Status: DESIGNED / STATICALLY VALIDATED**
> The pod lifecycles, PVC associations, and recovery markers described in this document reflect designed Kubernetes StatefulSet and CSI storage semantics. Static configuration and manifest integrity are verified by `scripts/validate.sh`. Live cluster execution output in this document is labeled as **ILLUSTRATIVE** to preserve strict evidence integrity.

---

## 1. Situation

A worker node running in our EKS cluster terminates abruptly due to an underlying hardware degradation. Among the pods running on that node is `stateful-demo-0`, an ordinal member of our stateful workload:

```text
NAME              READY   STATUS    RESTARTS   AGE     IP             NODE
stateful-demo-0   1/1     Running   0          42m     10.244.1.84    ip-10-0-1-12.ec2.internal
stateful-demo-1   1/1     Running   0          41m     10.244.2.19    ip-10-0-2-45.ec2.internal
```

The node goes `NotReady`, and the Kubernetes control plane evicts the uncontactable pod.

---

## 2. Assumption

The platform operator's first instinct is:

> *"Kubernetes will create another Pod, so the workload is fine."*

In a stateless `Deployment`, this assumption is valid. The Deployment controller creates a replacement pod with a new random name (e.g., `sample-api-7b8f9c6d4b-m4t9q`), the scheduler places it on any node with available CPU and memory, and traffic routing resumes once readiness checks succeed. The application does not care where it runs or what name it holds.

---

## 3. Hidden Problem

For a stateful workload, "create another Pod" is not enough.

If Kubernetes simply launched a fresh container:
- The replacement would have empty ephemeral disk space.
- Any historical transactional state, consensus logs, or local data written by `stateful-demo-0` prior to the crash would be lost.
- Other cluster members attempting to contact `stateful-demo-0` at its deterministic headless DNS name would reach an uninitialized process that lacks shard context.

The replacement process cannot merely restart; it must **recover state**. The system must maintain continuity across an entire chain of distinct abstractions:

```text
Old Pod Identity (stateful-demo-0)
      ↓
Replacement Pod Identity (stateful-demo-0)
      ↓
PersistentVolumeClaim (data-stateful-demo-0)
      ↓
PersistentVolume (pvc-7b91a0c4-...)
      ↓
Backing Block Storage (AWS EBS Volume)
      ↓
Filesystem Mount (/data)
      ↓
Application State Verification
      ↓
Readiness Probe Pass
```

If any link in this chain breaks, the replacement Pod will fail to serve traffic, regardless of whether its container phase is reported as `Running`.

---

## 4. Evidence (Illustrative Walkthrough)

To verify the stateful recovery path without guessing, an operator must inspect the diagnostic evidence at each layer of the chain:

### Step 1: Pod Ordinal Identity Preservation
Observe that the StatefulSet controller creates a replacement pod with the exact same ordinal name:

```bash
kubectl get pods -n sample-workloads -l app.kubernetes.io/name=stateful-demo -o wide
```

*Illustrative Output:*
```text
NAME              READY   STATUS    RESTARTS   AGE   IP             NODE
stateful-demo-0   0/1     Running   0          14s   10.244.3.112   ip-10-0-3-88.ec2.internal
stateful-demo-1   1/1     Running   0          43m   10.244.2.19    ip-10-0-2-45.ec2.internal
```

*Observation:* The replacement pod is scheduled to a different node (`ip-10-0-3-88`), receives a new IP address (`10.244.3.112`), but retains the exact ordinal name `stateful-demo-0`.

### Step 2: Persistent Volume Claim Association
Inspect the volume claim mounted by the replacement pod:

```bash
kubectl get pod stateful-demo-0 -n sample-workloads -o jsonpath='{.spec.volumes[?(@.name=="data")].persistentVolumeClaim.claimName}'
```

*Illustrative Output:*
```text
data-stateful-demo-0
```

*Observation:* The replacement pod mounts the exact claim `data-stateful-demo-0` that was created alongside the original pod.

### Step 3: Persistent Claim Status & PV Binding
Verify that the claim remained `Bound` throughout the pod's termination and rescheduling:

```bash
kubectl get pvc -n sample-workloads
```

*Illustrative Output:*
```text
NAME                   STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   AGE
data-stateful-demo-0   Bound    pvc-7b91a0c4-64b1-4c91-9e2a-1152a44d84f1   1Gi        RWO            gp3            45m
data-stateful-demo-1   Bound    pvc-3f82e811-9a72-4632-881c-99e821df22b8   1Gi        RWO            gp3            44m
```

*Observation:* The volume `pvc-7b91a0c4-...` was not deleted or reprovisioned. Its age (45m) confirms that the underlying cloud storage survived the pod's destruction.

### Step 4: Application State Verification
Inspect the application logs of the recovering container to verify that it discovered the historical marker:

```bash
kubectl logs stateful-demo-0 -n sample-workloads -c stateful-demo
```

*Illustrative Output:*
```text
=== Booting Stateful Replica stateful-demo-0 ===
State recovery: found existing persistent state for stateful-demo-0
Active persistent state content:
ordinal_identity=stateful-demo-0
created_at=2026-10-05T16:00:12Z
restarts=1
recovered_at=2026-10-05T16:42:26Z
last_boot=2026-10-05T16:42:26Z
Starting HTTP daemon on port 8080 serving /data
```

*Observation:* The application did not initialize a blank database. It detected `restarts=0`, incremented the counter to `restarts=1`, preserved the original creation timestamp (`16:00:12Z`), and recorded the recovery event timestamp (`16:42:26Z`).

### Step 5: Readiness Probe Verification
Confirm that the readiness probe successfully verified the persistent marker before exposing the pod to traffic:

```bash
kubectl get pod stateful-demo-0 -n sample-workloads
```

*Illustrative Output:*
```text
NAME              READY   STATUS    RESTARTS   AGE
stateful-demo-0   1/1     Running   0          38s
```

*Observation:* The readiness probe (`httpGet: /identity-marker.txt`) returned 200 OK. The pod is now marked `1/1 Ready` and accepts traffic.

---

## 5. Mechanism

Why did this recovery work?

1. **StatefulSet Controller Invariant:** Unlike a Deployment ReplicaSet, the StatefulSet controller guarantees that an ordinal index $i$ corresponds uniquely to Pod `<name>-<i>`. When Pod 0 is deleted, the controller strictly creates a Pod with ordinal 0.
2. **Deterministic PVC Association:** The `volumeClaimTemplates` specification dynamically creates PVCs following the naming convention `<template-name>-<statefulset-name>-<ordinal>`. When replacement Pod 0 is created, the controller binds it to the existing PVC named `data-stateful-demo-0`.
3. **Decoupled PVC Lifecycle:** By design, deleting a Pod does **not** delete its PVC. The PVC and its bound PV remain active in the cluster, preserving the underlying cloud block storage.
4. **CSI Detach and Reattach:** When Pod 0 was evicted from Node A, the Kubernetes `attachdetach-controller` sent an API call to the cloud provider (AWS EBS CSI driver) to detach the volume from Node A and attach it to Node B where the replacement Pod was scheduled.
5. **State Verification Probe:** The readiness probe directly validates that `/identity-marker.txt` is accessible via HTTP on port 8080. If volume mounting failed or the state file was unreadable, the container would remain unready (`0/1`), preventing premature client queries.

---

## 6. New Failure Boundary

While `StatefulSet` solves identity and persistent volume association, it creates a new operational failure boundary:

> **Storage-Bound Scheduling:**
> The replacement pod cannot run on just any node with spare compute. It can **only** run on nodes that have physical network and fabric access to the persistent volume.

If the volume is an AWS EBS volume located in Availability Zone `us-east-1a`, the replacement pod is physically locked to `us-east-1a`. If all worker nodes in `us-east-1a` are saturated or down, the replacement Pod remains stuck in `Pending`, even if `us-east-1b` and `us-east-1c` have 100 idle nodes.

---

## 7. Trade-off

| Benefit Gained | Operational Price Paid |
| :--- | :--- |
| **Data Continuity:** Pod crashes do not erase state. | **Slower Failover:** Attaching and detaching cloud block storage takes 30–90 seconds, compared to sub-second scheduling for stateless pods. |
| **Deterministic Addressing:** Stable DNS names allow peers to find each other without service discovery thrash. | **Inflexible Placement:** Workload placement is constrained by storage topology and zone boundaries. |
| **Automated Storage Binding:** No manual volume mapping or volume management scripts required. | **Operational Complexity:** Operators must understand CSI drivers, volume binding modes, and mount lifecycle diagnostics. |

---

## 8. Engineering Judgment

Do not assume a workload is resilient simply because it uses a StatefulSet.

The first useful operational question is not:

> *"Is the Pod Running?"*

The first useful question is:

> *"Did the expected state come back with it?"*

If the replacement Pod is running, but the application re-initialized an empty database or mounted the wrong volume, the system has suffered a silent state corruption event. True stateful reliability requires verifying the entire chain: ordinal identity $\longrightarrow$ PVC binding $\longrightarrow$ volume mount $\longrightarrow$ data integrity $\longrightarrow$ readiness.
