# ADR-004: Stateful Workload Strategy & Persistent Identity Model

## Status

Accepted

---

## Context

A Pod disappears.

For a stateless workload, the failure model is straightforward: Kubernetes creates another replica, the new container starts, and traffic resumes as soon as readiness checks pass. The new Pod has a completely different name, a different IP, and no memory of the past. It does not matter, because the application state lives elsewhere—in a managed database, an object store, or a distributed cache.

For a stateful workload, "create another Pod" is not enough.

When a stateful replica fails, the application replacement requires far more than bare compute. The replacement may require:
1. **The same logical identity:** Peers in a cluster (such as Raft or Paxos consensus groups) or partitioned clients must know exactly which node has returned.
2. **The correct persistent storage:** The replacement must reattach to its own specific data volume, not an arbitrary unformatted volume or the volume belonging to a different replica.
3. **Storage attachment and mounting:** The cloud block storage volume must be detached from the previous host node (which may have crashed abruptly) and attached to the new host node.
4. **Successful application-level state recovery:** The application must verify filesystem integrity, replay write-ahead logs (WAL), and reconstruct in-memory indexes before declaring itself functional.
5. **Readiness evaluation:** The replica must not accept client traffic or peer coordination until its state is verified.

This changes the fundamental failure model of the platform:

```text
Milestone #21 (Workload Probes)
Question: "Can this specific replica serve traffic?"
└── Decouples container running state from ready backend routing.

            ↓

Milestone #22 (PodDisruptionBudget)
Question: "Can voluntary disruption proceed safely?"
└── Guarantees numerical serving capacity during planned maintenance.

            ↓

Milestone #23 (Topology Resilience)
Question: "Will another replica survive the failure domain we designed for?"
└── Guarantees spatial distribution across physical Availability Zones.

            ↓

Milestone #24 (Stateful Workloads)
Question: "Can the workload recover with the correct identity and state?"
└── Couples compute lifecycle to stable network identity and persistent storage.
```

---

## Problem

A frequent operational misconception in platform engineering is:

$$
\text{Pod Replacement} = \text{State Recovery}
$$

Or even more dangerously:

$$
\text{StatefulSet} = \text{Automatic High Availability}
$$

When platform teams deploy stateful workloads into Kubernetes, they encounter distinct architectural pitfalls:

1. **Fungible vs. Non-Fungible Replicas:** Stateless workloads assume replicas are interchangeable. Stateful workloads have non-fungible roles (e.g., node-0 is shard leader, node-1 is follower). Standard Deployments assign random hash suffixes and cannot guarantee stable per-replica storage association across restarts.
2. **Storage-Compute Coupling:** In Kubernetes, Pods are ephemeral, but state must be durable. Decoupling storage lifecycle from Pod lifecycle requires specific controller mechanisms (`volumeClaimTemplates`). Without these, deleting a Pod destroys its local data.
3. **Storage Topology Constraints:** In Milestone #23, we established multi-AZ topology spread for compute. However, cloud block storage (e.g., AWS EBS) is physically constrained to a single Availability Zone. A volume created in `us-east-1a` cannot be attached to a node in `us-east-1b`. Workload placement becomes constrained not just by available CPU and memory, but by physical storage attachment topology.
4. **The False Sense of High Availability:** Merely running a StatefulSet with 3 replicas does not mean data is replicated, does not mean failover is automatic, and does not replace backup and disaster recovery.

---

## Decision

We adopt the **`StatefulSet`** controller and **Headless Service** (`clusterIP: None`) for stateful workloads where application correctness requires stable network identity and dedicated, persistent volume association across Pod replacements.

We implement this strategy via a minimal, self-contained reference workload in [`kubernetes/workloads/stateful-demo/`](../../kubernetes/workloads/stateful-demo/):

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: stateful-demo
  namespace: sample-workloads
spec:
  serviceName: stateful-demo-headless
  replicas: 2
  podManagementPolicy: OrderedReady
  updateStrategy:
    type: RollingUpdate
  selector:
    matchLabels:
      app.kubernetes.io/name: stateful-demo
  template:
    ...
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes:
          - ReadWriteOnce
        resources:
          requests:
            storage: 1Gi
```

### Core Architecture Decisions

#### 1. Stable Ordinal Identity & Headless Service
- **Ordinal Naming:** The StatefulSet controller assigns deterministic ordinal names (`stateful-demo-0`, `stateful-demo-1`). When Pod `stateful-demo-0` dies, its replacement is named `stateful-demo-0`.
- **Deterministic Network Identity:** The Headless Service (`clusterIP: None`) bypasses virtual IP load balancing. CoreDNS registers individual `A`/`SRV` records for each ordinal replica:
  $$\text{stateful-demo-0}.\text{stateful-demo-headless}.\text{sample-workloads}.\text{svc}.\text{cluster}.\text{local}$$
- **Why It Matters:** Peers and stateful clients can address specific replicas directly, enabling leader election, replication pipelines, and deterministic data sharding.

#### 2. Dedicated Persistent Volumes via `volumeClaimTemplates`
- Rather than sharing a single volume or mounting host paths, `volumeClaimTemplates` provisions a dedicated PersistentVolumeClaim (PVC) for each ordinal identity:
  - `stateful-demo-0` $\longrightarrow$ `data-stateful-demo-0`
  - `stateful-demo-1` $\longrightarrow$ `data-stateful-demo-1`
- When `stateful-demo-0` is terminated, its PVC is **not** deleted. When the replacement `stateful-demo-0` is created, the StatefulSet controller rebinds `data-stateful-demo-0` to the new Pod.

#### 3. Access Mode: `ReadWriteOnce`
- We configure `accessModes: [ReadWriteOnce]`.
- *Semantic Precision:* `ReadWriteOnce` (RWO) specifies that the volume can be mounted as read-write by a **single node**. It is a node-level attachment restriction, not a guarantee that only one Pod can write.
- We deliberately avoid `ReadWriteMany` (RWX) for this baseline because distributed network filesystems (e.g., NFS, AWS EFS) introduce network latency, locking complexities, and POSIX compliance quirks unsuited for high-throughput transactional storage.

#### 4. StorageClass Portability
- We deliberately omit a hardcoded `storageClassName` (e.g., AWS account-specific or vendor-specific storage classes) to allow the workload to bind to the cluster's **default StorageClass** (`gp2`/`gp3` on AWS EKS, `standard` on KIND or Minikube).
- This ensures reproducible local testing without coupling the platform manifests to proprietary AWS account IDs or environment-specific CSI provisioners.

#### 5. Storage Topology: `WaitForFirstConsumer`
- We establish the operational requirement that storage classes backing stateful workloads must use:
  $$\text{volumeBindingMode}: \text{WaitForFirstConsumer}$$
- *Rationale:* Under `Immediate` binding, the volume is provisioned as soon as the PVC is created, before the Pod is scheduled. If the storage provisioner arbitrarily creates the volume in Availability Zone A, but the node scheduler needs to place the Pod in Zone B (to satisfy compute constraints or Milestone #23 topology spread), the Pod is trapped in a permanent `Pending` state. `WaitForFirstConsumer` delays volume provisioning until the scheduler picks an eligible node, guaranteeing the volume is created in the same physical zone as the scheduled Pod.

#### 6. PVC Lifecycle & Retention Policy
- We explicitly rely on the Kubernetes default retention behavior: **PVCs are retained when Pods or StatefulSets are deleted or scaled down**.
- *Rationale:* In a production reliability engineering model, data preservation takes absolute precedence over automatic disk reclamation. Accidental StatefulSet deletion or scale-down must never result in silent, irreversible data loss.

---

## Decision Drivers

- **Deterministic Identity:** Applications requiring clustering, quorum, or dedicated state cannot tolerate arbitrary random pod hashes.
- **State Survival Across Replacement:** Pods must be free to crash, be evicted, or undergo node patching without losing their underlying data history.
- **Storage Topology Alignment:** Ensure volume provisioning cooperates with the multi-AZ scheduling invariants established in Milestone #23.
- **Operational Diagnostic Clarity:** Clearly separate node compute failures from storage attachment and mount failures.

---

## Alternatives Evaluated

| Strategy | Architectural Model | Trade-offs & Limitations | Verdict |
| :--- | :--- | :--- | :--- |
| **A. Deployment + Ephemeral Storage (`emptyDir`)** | Pod writes to local ephemeral node disk. Replicas are treated as fungible. | Zero storage cost. Fast replacement. However, all data is permanently lost when the Pod restarts or migrates to another node. Cannot provide persistent state semantics. | **Rejected for stateful workloads** (Valid only for stateless caches) |
| **B. Deployment + Shared Persistent Storage (`ReadWriteMany`)** | Multiple Deployment replicas mount a single shared volume (e.g., AWS EFS or NFS). | Replicas can access the same shared files, but replicas lack unique identities. High network storage latency, file locking contention, and POSIX consistency issues. Does not provide per-replica shard isolation. | **Rejected for baseline** (High complexity; poor performance for transactional data) |
| **C. StatefulSet + `volumeClaimTemplates`** | Dedicated per-replica PVC, stable ordinal names, headless DNS, independent storage lifecycle. | Solves identity and persistent state association. However, couples Pod scheduling to storage topology and introduces attachment/mount failure boundaries. | **Accepted** (Demonstrates exact stateful recovery mechanics) |
| **D. External / Managed State (e.g., AWS RDS / DynamoDB)** | Application runs as a stateless Deployment in EKS; all persistent state is offloaded to a managed cloud database. | Offloads high availability, automated backup, cross-AZ replication, and failover to cloud provider. Incurs higher service costs, network latency, and vendor lock-in. | **Valid Architectural Alternative** (Widely used in production, but does not exercise Kubernetes-native stateful mechanics) |

---

## The Identity & Storage Model

```text
StatefulSet: stateful-demo (replicas: 2)
  ├── Pod 0: stateful-demo-0
  │     ├── Hostname: stateful-demo-0
  │     ├── DNS: stateful-demo-0.stateful-demo-headless.sample-workloads.svc.cluster.local
  │     ├── PVC: data-stateful-demo-0
  │     └── Mounted Path: /data (PersistentVolume pvc-xxxx)
  └── Pod 1: stateful-demo-1
        ├── Hostname: stateful-demo-1
        ├── DNS: stateful-demo-1.stateful-demo-headless.sample-workloads.svc.cluster.local
        ├── PVC: data-stateful-demo-1
        └── Mounted Path: /data (PersistentVolume pvc-yyyy)
```

### The Invariant: Pod Ordinal to Storage Mapping
The formula for StatefulSet PVC naming is deterministic:

$$\text{PVC Name} = \langle\text{claim-template-name}\rangle\text{-}\langle\text{statefulset-name}\rangle\text{-}\langle\text{ordinal}\rangle$$

For our workload:
- Template `name: data` + Pod `stateful-demo-0` $\longrightarrow$ `data-stateful-demo-0`
- Template `name: data` + Pod `stateful-demo-1` $\longrightarrow$ `data-stateful-demo-1`

When `stateful-demo-0` is deleted, `data-stateful-demo-0` remains untouched in etcd and in the storage backend. When the controller reconciles and spawns replacement `stateful-demo-0`, it requests the exact claim `data-stateful-demo-0`, attaching the original volume back to the replacement Pod.

---

## Storage Lifecycle: PVC Retention vs. PV Reclaim Policy

Platform engineers must distinguish between two completely independent storage lifecycle controls:

```text
┌────────────────────────────────────────────────────────┐
│             StatefulSet PVC Retention Policy           │
│      (Controls what happens to PVC when STS changes)   │
└───────────────────────────┬────────────────────────────┘
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
          Retain (Default)              Delete
    PVC remains in cluster etcd    PVC object is deleted from etcd
              │                           │
              │                           ▼
              │             ┌─────────────────────────────────────────┐
              │             │      StorageClass PV Reclaim Policy     │
              │             │  (Controls backing storage when PVC dies)│
              │             └─────────────────────┬───────────────────┘
              │                                   │
              │                     ┌─────────────┴─────────────┐
              │                     ▼                           ▼
              │                   Retain                      Delete
              ▼             PV enters "Released"        Underlying cloud volume
    Data completely safe    Data remains on cloud disk   (EBS) is destroyed permanently!
```

1. **StatefulSet PVC Retention (`persistentVolumeClaimRetentionPolicy`):**
   - Introduced in Kubernetes v1.27 (GA in v1.32).
   - Controls whether Kubernetes deletes PVC objects when a StatefulSet is deleted or scaled down.
   - We maintain `Retain` (the baseline default) so that neither manual deletions nor automated scaling destroy persistent claims.
2. **PersistentVolume Reclaim Policy (`reclaimPolicy`):**
   - Configured on the `StorageClass` or `PersistentVolume` (`Delete` vs. `Retain`).
   - Governs what happens to the physical storage disk (e.g., AWS EBS volume) when the **PVC object** is deleted.
   - If the StorageClass uses `reclaimPolicy: Delete`, deleting a PVC permanently deletes the backing cloud disk. If `reclaimPolicy: Retain`, the PV transitions to `Released` status, preserving the disk until an operator manually reclaims or deletes it.

> [!WARNING]
> **Critical Safety Boundary:** "PVC Retained" does **not** mean underlying storage can never be deleted. If an operator or automated script manually runs `kubectl delete pvc data-stateful-demo-0` on a StorageClass with `reclaimPolicy: Delete`, the physical AWS EBS volume and all application data are immediately destroyed.

---

## Failure Boundaries & Non-Goals

1. **StatefulSet Does Not Replicate Data:**
   - A StatefulSet provisions storage and identity. It does **not** copy bytes between volumes.
   - If `stateful-demo-0` writes data to `data-stateful-demo-0`, that data exists **only** on Volume 0. Pod 1 (`stateful-demo-1`) cannot read or access Volume 0.
   - Application-level replication (e.g., MySQL binary logging, PostgreSQL streaming replication, or Redis replication streams) must be implemented by the application itself.
2. **StatefulSet Does Not Provide Automatic Failover:**
   - If a primary database replica crashes, the StatefulSet controller will recreate the Pod. It will **not** promote a secondary replica to primary or reconfigure client connections.
   - Failover requires application clustering logic or specialized Kubernetes Operators (e.g., CloudNativePG, Strimzi Kafka Operator).
3. **Persistent Volume Does Not Equal Backup:**
   - A live EBS volume attached to a running Pod protects against Pod crashes, but provides **zero protection** against:
     - Accidental `DROP TABLE` or data corruption.
     - Volume filesystem corruption.
     - Entire AWS Availability Zone catastrophic destruction.
   - Independent backup strategies (volume snapshots, off-site object backups via AWS S3) remain mandatory.
4. **Multi-AZ Pods Do Not Mean Multi-AZ Storage:**
   - Spreading replicas across Availability Zones (Milestone #23) provides compute redundancy.
   - However, standard cloud block storage volumes are bound to a single AZ. If Availability Zone A suffers an outage, the EBS volume in Zone A is inaccessible until Zone A recovers. A replacement Pod cannot mount that volume in Zone B.

---

## Consequences

### Positive
- **Predictable Recovery:** Pod replacements automatically reacquire their designated ordinal identity and persistent storage.
- **Stable Network Addressing:** Headless Service provides permanent DNS endpoints for individual replicas, facilitating clustering protocols and targeted client routing.
- **Decoupled Durability:** Compute containers can be terminated, drained, or patched without destroying persistent application state.
- **Accurate Readiness Gate:** Readiness probes can inspect storage mount validity and data integrity before allowing traffic into a recovering replica.

### Negative / Trade-offs
- **Complex Recovery Dependencies:** Workload recovery now depends on the CSI storage subsystem, cloud provider API attachment limits, and physical volume detachment times.
- **Zone Affinity Restrictions:** Pods bound to EBS volumes lose placement freedom. They cannot fail over to a healthy Availability Zone if their assigned zone loses compute capacity.
- **Multi-Attach Failures:** If a worker node crashes abruptly without unmounting its volumes, the cloud provider may refuse to attach the volume to a new node until the node lock times out (typically 6–10 minutes), trapping the replacement Pod in `ContainerCreating`.
- **Scaling Complexity:** StatefulSets cannot be scaled up or down arbitrarily without considering application clustering rebalancing and state distribution.

---

## Relationship to Prior Milestones

- **Milestone #21 (Workload Probes):** For stateless workloads, readiness verified that HTTP ports were responding. For stateful workloads, readiness must verify that the persistent volume is mounted and storage integrity checks have passed.
- **Milestone #22 (PodDisruptionBudget):** Draining a node hosting a stateful replica requires careful PDB calibration. Evicting a stateful pod triggers cloud volume detachment and reattachment on another node, which incurs higher recovery latency than stateless pod rescheduling.
- **Milestone #23 (Topology Resilience):** Compute topology spread ensures replicas land in different zones. However, stateful workloads require `WaitForFirstConsumer` so that volume provisioning respects compute topology, preventing un-schedulable volume-zone mismatches.
- **Milestone #24 (Stateful Workloads):** Establishes the identity and persistent storage layer, demonstrating that Pod replacement is only the first step in stateful recovery.
