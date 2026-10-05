# Stateful Workload Recovery & Identity Architecture

This architecture document visualizes how Kubernetes manages workload identity, network addressing, and persistent storage association during StatefulSet Pod replacement, and models the failure boundaries that emerge when persistent state becomes coupled to compute lifecycles.

Engineering thesis:

$$
\text{Stateless Replica} \longrightarrow \text{Replaceable}
$$

$$
\text{Stateful Replica} \longrightarrow \text{Recoverable (Identity + State)}
$$

> "A stateless replica can often be replaced. A stateful replica may need to be recovered with the correct identity and the correct data."
>
> "Pod replacement is not the same as state recovery."

---

## The Recovery Model: What Survived When the Pod Did Not?

A Pod disappears.

For a stateless workload like `sample-api`, the failure model is straightforward: the Deployment controller observes the missing replica, creates another Pod with an arbitrary random hash identity, schedules it to any node with available compute, and resumes routing once the readiness probe passes.

For a stateful workload, "create another Pod" is not enough. Replicas are not fungible commodities. A stateful replacement requires a coordinated recovery chain:

```text
Pod Ordinal (stateful-demo-0)
      ↓
Network Identity (<pod>.<service>.sample-workloads.svc.cluster.local)
      ↓
PersistentVolumeClaim (data-stateful-demo-0)
      ↓
PersistentVolume / Backing Block Storage (EBS / host / SAN)
      ↓
Node Volume Attachment & Filesystem Mount (/data)
      ↓
Application State Verification & Recovery
      ↓
Readiness Evaluation (Traffic Eligibility)
```

The diagram below contrasts the normal state, the intended recovery path, and the second failure boundary where a replacement Pod exists but state cannot be recovered.

> [!NOTE]
> **Evidence Status: DESIGNED / ILLUSTRATIVE**
> The object names, controller reconciliation steps, and failure boundaries below illustrate Kubernetes StatefulSet and CSI storage semantics. They represent the architectural model implemented by `stateful-demo`.

---

## StatefulSet Identity & Recovery Flow

```mermaid
flowchart TD
    subgraph NORMAL_STATE["1. Normal Operating State (Steady-State Association)"]
        direction TB
        STS["StatefulSet: stateful-demo\n(replicas: 2, serviceName: stateful-demo-headless)"]
        HeadlessSvc["Headless Service: stateful-demo-headless\n(clusterIP: None)"]
        STS -.->|"Governs DNS domain"| HeadlessSvc

        subgraph POD0_STEADY["Ordinal Identity 0"]
            Pod0["Pod: stateful-demo-0\n(IP: 10.244.1.15, Node: node-az-1a)\nDNS: stateful-demo-0.stateful-demo-headless"]
            PVC0["PVC: data-stateful-demo-0\n(Bound, ReadWriteOnce)"]
            PV0["PV: pvc-7b91a...\n(StorageClass: default, Zone: us-east-1a)"]
            Disk0[("Backing Storage Volume\n(e.g., AWS EBS gp3 volume)")]
            DataMarker0["Application State:\n/data/identity-marker.txt\nordinal=stateful-demo-0, restarts=0"]

            Pod0 -->|"Mounts /data"| PVC0
            PVC0 -->|"Bound to"| PV0
            PV0 -->|"Represents"| Disk0
            Disk0 -->|"Persists"| DataMarker0
        end
    end

    subgraph DISRUPTION["2. Node Failure or Voluntary Eviction"]
        direction TB
        PodCrash["Pod Disappears\n(Node failure, eviction, or manual deletion)"]
        Pod0 -.->|"Lost"| PodCrash
        DataSurvives["WHAT SURVIVED:\n• PVC: data-stateful-demo-0 (Retained in etcd)\n• PV & Cloud Storage Volume (Intact)\n• Persisted Data Marker (Intact)"]
        PodCrash -.->|"Data remains intact"| DataSurvives
    end

    subgraph RECOVERY_PATH["3. Intended Recovery Path (Identity & State Reassociation)"]
        direction TB
        ObsFilter["StatefulSet Controller observes:\nDesired: 2 replicas | Current: 1 (ordinal 0 missing)"]
        RecreatePod["Controller creates replacement Pod:\nName: stateful-demo-0 (Fixed Ordinal Identity)\nPreserves headless DNS hostname"]
        Scheduler["kube-scheduler VolumeBindingChecker:\nMust schedule to node with access to existing PV\n(Zone-pinned if EBS volume)"]
        Reattach["Attach/Detach Controller & CSI:\nAttach backing volume to new node -> Mount to /data"]
        BootCheck["Container Boot Script:\nInspects /data -> Finds existing identity-marker.txt\nIncrements restarts -> Preserves historical data"]
        ProbePass["Readiness Probe:\nHTTP GET /identity-marker.txt -> 200 OK\nState verified -> Endpoint marked Ready"]

        ObsFilter --> RecreatePod
        RecreatePod --> Scheduler
        Scheduler --> Reattach
        Reattach --> BootCheck
        BootCheck --> ProbePass
    end

    subgraph SECOND_FAILURE_BOUNDARY["4. The Second Failure Boundary (Pod Created != State Recovered)"]
        direction TB
        FailPod["Replacement Pod stateful-demo-0 Created\n(Pod exists in cluster)"]

        subgraph FAIL_MODES["Root Causes of State Recovery Failure"]
            FailAttach["Volume Attachment Failure\n(Multi-Attach error / volume locked to old failed node)"]
            FailZone["Topology Constraint Failure\n(Volume pinned to AZ-1a, but AZ-1a has no schedulable nodes)"]
            FailMount["Filesystem Corruption / Mount Failure\n(CSI driver unable to format or mount filesystem)"]
            FailApp["Application Recovery Crash\n(Corrupted WAL / unreadable state marker / permissions lock)"]
        end

        FailPod --> FailAttach
        FailPod --> FailZone
        FailPod --> FailMount
        FailPod --> FailApp

        FailOutcome["Result: Pod stuck in Pending / ContainerCreating / CrashLoopBackOff\nreadiness: false -> Service traffic blocked\nDIAGNOSTIC EVIDENCE REQUIRED"]
        FailAttach --> FailOutcome
        FailZone --> FailOutcome
        FailMount --> FailOutcome
        FailApp --> FailOutcome
    end

    NORMAL_STATE --> DISRUPTION
    DISRUPTION --> RECOVERY_PATH
    DISRUPTION -.-> SECOND_FAILURE_BOUNDARY
```

---

## Architectural Breakdown

### 1. Stable Ordinal Identity vs. Ephemeral Identity

In a stateless `Deployment`, pods are assigned random string suffixes (e.g., `sample-api-7b8f9c6d4b-xk92m`). When a pod is deleted, the replacement receives a completely new random hash (e.g., `sample-api-7b8f9c6d4b-m4t9q`). The two pods share no historical relationship.

In a `StatefulSet`:
- Each pod receives an immutable integer ordinal: `stateful-demo-0`, `stateful-demo-1`.
- When `stateful-demo-0` terminates, the controller strictly creates another pod named `stateful-demo-0`.
- The replacement pod inherits the exact same network identity and PersistentVolumeClaim mapping.

> [!IMPORTANT]
> **Semantic Precision:** The same Pod object does **not** "come back." Pods remain ephemeral, replaceable Kubernetes objects. What survives is the **declared ordinal identity** and the **underlying PersistentVolumeClaim**. The StatefulSet controller guarantees that the replacement pod is granted that identical identity and reassociated with that specific storage claim.

---

### 2. Network Identity via Headless Service

A standard Kubernetes ClusterIP Service provides a virtual load-balancing IP (`VIP`) that routes incoming traffic to any ready pod behind its selector. This abstraction is optimal for stateless replicas, where every pod provides identical functionality.

Stateful workloads require direct, deterministic addressing to individual members:
- A primary database replica must be distinguishable from read-only standby replicas.
- Distributed consensus protocols (Raft, Paxos) require stable peer addresses to maintain quorum.
- Stateful clients may need to route queries directly to the node hosting specific partitioned shards.

Configuring `clusterIP: None` creates a **Headless Service**. CoreDNS provisions deterministic `SRV` and `A` records for each pod:

$$\text{stateful-demo-0}.\text{stateful-demo-headless}.\text{sample-workloads}.\text{svc}.\text{cluster}.\text{local} \longrightarrow \text{Pod 0 IP}$$

$$\text{stateful-demo-1}.\text{stateful-demo-headless}.\text{sample-workloads}.\text{svc}.\text{cluster}.\text{local} \longrightarrow \text{Pod 1 IP}$$

When `stateful-demo-0` is rescheduled to a new node with a different IP address, CoreDNS automatically updates the `A` record for `stateful-demo-0.stateful-demo-headless` to the new IP address. The logical DNS name remains constant.

---

### 3. Storage Coupling & The Topology Boundary

In Milestone #23, we evaluated compute topology: *"Where can the Pod run?"*

Milestone #24 adds a tighter constraint: *"Where can the Pod run AND still access its state?"*

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        Availability Zone: us-east-1a                   │
│                                                                        │
│   Worker Node A                          AWS EBS Volume gp3            │
│   ┌─────────────────────┐                ┌─────────────────────────┐   │
│   │ Pod:                │ ── attaches ── │ PV: pvc-7b91a...        │   │
│   │ stateful-demo-0     │    via CSI     │ (data-stateful-demo-0)  │   │
│   └─────────────────────┘                └─────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
                                    ▲
                                    │ CANNOT ATTACH ACROSS ZONES
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        Availability Zone: us-east-1b                   │
│                                                                        │
│   Worker Node B                                                        │
│   ┌─────────────────────┐                                              │
│   │ Candidate Node      │ ──X (EBS is physically restricted to 1a)    │
│   └─────────────────────┘                                              │
└────────────────────────────────────────────────────────────────────────┘
```

1. **Zone-Bound Block Storage:** Cloud block storage volumes (such as Amazon EBS) are physically constrained to a single Availability Zone. An EBS volume created in `us-east-1a` cannot be attached to an EC2 instance running in `us-east-1b`.
2. **Scheduler Topology Pinning:** Once a persistent volume is provisioned in Zone A, the `kube-scheduler`'s `VolumeBindingChecker` plugin evaluates the node topology against the volume's `nodeAffinity`. Even if Zone B has abundant idle compute, the scheduler **must** place replacement `stateful-demo-0` onto a node in Zone A.
3. **Volume Binding Modes:**
   - **`VolumeBindingMode: Immediate`:** The PersistentVolume is provisioned as soon as the PVC is created, before any Pod is scheduled. If the provisioner arbitrarily selects Zone B, but the workload has affinity or constraints for Zone A, the Pod is permanently stuck in `Pending`.
   - **`VolumeBindingMode: WaitForFirstConsumer`:** Delays PV provisioning until the Pod is actually scheduled. The scheduler selects an eligible node first (evaluating compute, taints, and topology constraints), and the storage provisioner then creates the volume in the node's specific Availability Zone.

---

### 4. The Two Failure Boundaries of Stateful Workloads

| Failure Boundary | Symptom | What Failed | Diagnostic Location |
| :--- | :--- | :--- | :--- |
| **Boundary 1: Orchestration Failure** | Pod does not schedule or does not appear | Controller unable to reconcile, or scheduler cannot satisfy node resource/topology constraints | `kubectl describe pod` -> `Events` (`FailedScheduling`) |
| **Boundary 2: Storage Recovery Failure** | Pod exists, but remains `ContainerCreating`, `Pending`, or `CrashLoopBackOff` | Volume cannot attach (locked to dead node), cannot mount (filesystem issue), or application crashes during WAL replay | `kubectl describe pod` -> `FailedAttachVolume`, `FailedMount`, or `kubectl logs` |

This distinction drives our entire operational model: **Pod Running $\ne$ State Recovered.**
