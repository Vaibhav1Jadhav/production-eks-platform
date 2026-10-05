# Failure Scenario 2: Volume Attachment & Mount Failure

## Status: DESIGNED / ILLUSTRATIVE

> [!NOTE]
> **Evidence Status: DESIGNED / ILLUSTRATIVE**
> The event streams, pod phases, and CSI error messages in this scenario illustrate common failure modes encountered when cloud block storage is detached and reattached during StatefulSet pod replacement. All logs and event excerpts are marked as **ILLUSTRATIVE**.

---

## 1. Situation

A stateful pod `stateful-demo-0` running on worker node `ip-10-0-1-12` suddenly loses connectivity because the underlying EC2 instance kernel panics.

The Kubernetes node controller marks the node `NotReady` after 40 seconds. After the pod eviction timeout (default 5 minutes) expires, the StatefulSet controller creates a replacement Pod for `stateful-demo-0`:

```bash
kubectl get pods -n sample-workloads -l app.kubernetes.io/name=stateful-demo -o wide
```

*Illustrative Output:*
```text
NAME              READY   STATUS              RESTARTS   AGE    IP       NODE
stateful-demo-0   0/1     ContainerCreating   0          8m     <none>   ip-10-0-1-95.ec2.internal
stateful-demo-1   1/1     Running             0          54m    ...      ip-10-0-2-45.ec2.internal
```

The replacement Pod has been scheduled to healthy node `ip-10-0-1-95`. However, it remains trapped in the `ContainerCreating` phase for over 8 minutes.

---

## 2. Assumption

The platform operator observes:

> *"The replacement Pod was successfully created and assigned to a healthy node. Kubernetes will start the container any second."*

The assumption is that once scheduling succeeds, container initialization is practically instantaneous.

---

## 3. Hidden Problem

The replacement Pod exists, but the application is completely dead.

Unlike stateless pods that pull an image and launch immediately, stateful pods cannot start their containers until the underlying block volume is physically detached from the old host, attached to the new host, and mounted by the node OS.

When the previous worker node crashes hard without performing a clean shutdown:
1. **The Cloud Volume Remains Attached to the Dead Node:** In AWS, an EBS volume can only be attached to one EC2 instance at a time under `ReadWriteOnce`. The AWS control plane still records the volume as attached to `i-0123456789deadbeef` (the crashed instance).
2. **Multi-Attach Lockout:** When Kubernetes requests AWS to attach `vol-0987654321` to `ip-10-0-1-95` (the new instance), the cloud provider rejects the request with a **Multi-Attach error**.
3. **Detachment Timeout Delay:** The Kubernetes `attachdetach-controller` must wait for an internal force-detach timeout (which can take between 6 and 10 minutes) before forcefully signaling the cloud provider to break the attachment lock on the dead node.

During this entire window, the pod remains in `ContainerCreating`, the container never boots, no readiness probe can run, and the stateful replica is completely unavailable.

---

## 4. Evidence (Illustrative Walkthrough)

To diagnose why a stateful pod is stuck in `ContainerCreating`, an operator must inspect the Pod's lifecycle events:

### Step 1: Capture Pod Events
Inspect the events on the stuck replacement Pod:

```bash
kubectl describe pod stateful-demo-0 -n sample-workloads
```

*Illustrative Event Output:*
```text
Events:
  Type     Reason                  Age               From                     Message
  ----     ------                  ----              ----                     -------
  Normal   Scheduled               8m12s             default-scheduler        Successfully assigned sample-workloads/stateful-demo-0 to ip-10-0-1-95.ec2.internal
  Warning  FailedAttachVolume      8m10s             attachdetach-controller  Multi-Attach error for volume "pvc-7b91a0c4-64b1-4c91-9e2a-1152a44d84f1" Volume is already exclusively attached to one node and can't be attached to another
  Warning  FailedMount             6m08s (x4 over 8m) kubelet                  Unable to attach or mount volumes: timed out waiting for the condition
  Warning  FailedAttachVolume      4m02s             attachdetach-controller  AttachVolume.Attach failed for volume "pvc-7b91a0c4-64b1-4c91-9e2a-1152a44d84f1" : rpc error: code = Internal desc = VolumeAttachment (attachment-0a1b2c3d) is already attached to node ip-10-0-1-12
```

*Observation:* The event stream clearly identifies `FailedAttachVolume` with a `Multi-Attach error`. The CSI driver cannot complete the attachment because the volume is still recorded as attached to the previous crashed node (`ip-10-0-1-12`).

### Step 2: Inspect VolumeAttachment Objects
Inspect the Kubernetes internal `VolumeAttachment` API to observe the attachment state:

```bash
kubectl get volumeattachment
```

*Illustrative Output:*
```text
NAME                                                                 ATTACHER                 PV                                         NODE                        ATTACHED   AGE
csi-7b91a0c464b14c919e2a1152a44d84f1-ip-10-0-1-12.ec2.internal       ebs.csi.aws.com          pvc-7b91a0c4-64b1-4c91-9e2a-1152a44d84f1   ip-10-0-1-12.ec2.internal   true       55m
csi-7b91a0c464b14c919e2a1152a44d84f1-ip-10-0-1-95.ec2.internal       ebs.csi.aws.com          pvc-7b91a0c4-64b1-4c91-9e2a-1152a44d84f1   ip-10-0-1-95.ec2.internal   false      8m
```

*Observation:* Two attachment requests exist simultaneously for the same PV:
- The old node (`ip-10-0-1-12`) is recorded as `ATTACHED: true`.
- The new node (`ip-10-0-1-95`) is recorded as `ATTACHED: false`.

The CSI attacher is blocked because the cloud block device is physically locked.

---

## 5. Mechanism

What makes volume attachment fail during stateful pod replacement?

1. **Node Failure Detection Gap:** Kubernetes uses heartbeats (`NodeStatus`) to detect node health. When a node stops responding, the control plane cannot determine whether the node is dead, rebooting, or merely network-partitioned.
2. **Preventing Split-Brain / Data Corruption:** If Kubernetes immediately attached the volume to the new node while the old node was still running (e.g., during a temporary network partition), both nodes might write to the same filesystem simultaneously, resulting in catastrophic filesystem and database corruption.
3. **Cloud Controller Serialization:** Cloud block storage providers (AWS EBS, GCP Persistent Disk, Azure Managed Disks) enforce strict single-instance attachment locks for volumes created under `ReadWriteOnce`. The cloud API will reject any attachment request until the volume is fully detached.
4. **Attach/Detach Controller Reconciler:** Kubernetes will not forcefully detach a volume from a `NotReady` node until the `volume.beta.kubernetes.io/storage-provisioner` timeout or node termination is confirmed by the cloud controller manager.

---

## 6. New Failure Boundary

This scenario reveals the fundamental operational reality of stateful workloads:

> **The Storage Attachment Boundary:**
> Pod creation is a control plane metadata operation (instantaneous).
> Volume attachment is a physical cloud infrastructure operation (slow, serialized, and subject to hardware timeouts).

A stateful pod replacement is not complete when the Pod enters `Pending` or `ContainerCreating`. It is only complete when:
1. The old attachment is severed.
2. The cloud provider updates its storage fabric.
3. The new worker node attaches the block device.
4. The local kubelet formats or mounts the filesystem to the container's mount path.
5. The container processes start and pass readiness probes.

---

## 7. Trade-off

| Design Choice | Operational Consequence |
| :--- | :--- |
| **Strict Single-Node Mounting (`ReadWriteOnce`)** | Prevents concurrent writes and eliminates filesystem corruption risk. However, failover incurs a multi-minute delay when a host node crashes abruptly. |
| **Shared Network Storage (`ReadWriteMany` / EFS)** | Eliminates volume attachment delay (multiple nodes can mount simultaneously). However, introduces network latency, file-locking overhead, and degraded transactional performance. |
| **Aggressive Node Force-Deletion** | Force-deleting a `NotReady` node breaks the attachment lock faster. However, if the old node was merely partitioned and still running, it can cause severe data corruption. |

---

## 8. Engineering Judgment

Never conflate Pod existence with application availability.

When a stateful replica fails:
- Do not immediately panic or delete replacement pods when they linger in `ContainerCreating`. Deleting the replacement pod merely resets the controller loop and lengthens the recovery time.
- Check `kubectl describe pod` for `FailedAttachVolume` and `Multi-Attach error`.
- Verify the status of the crashed worker node. If the node is confirmed terminated in AWS EC2, deleting the Kubernetes `Node` object allows the `attachdetach-controller` to immediately release the volume lock and complete the attachment to the new node.

The engineering lesson:

> **"Replacement Pod created" does not mean "state recovered."**
