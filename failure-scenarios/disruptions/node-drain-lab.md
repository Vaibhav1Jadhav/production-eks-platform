# Hands-On Lab: Can This Workload Survive a Node Drain?

This hands-on laboratory walks platform engineers and SREs through a controlled, observable experiment testing how the Kubernetes **Eviction API** and `PodDisruptionBudget` govern planned node disruptions.

> [!CAUTION]
> **RUN ONLY ON A DISPOSABLE / TEST CLUSTER (e.g., local kind cluster, Minikube multi-node, or dedicated EKS sandbox).**  
> Draining nodes cordons worker nodes and evicts running pods. Never perform destructive node drain testing against production or shared staging environments.

---

## Lab Objectives

1. Observe the mathematical behavior of `minAvailable: 2` on a 3-replica workload during a real node drain.
2. Watch `disruptionsAllowed` drop from `1` to `0` during eviction, and verify replacement pod probe warm-up.
3. Intentionally inject an over-restrictive budget (`minAvailable: 3`) to reproduce an eviction deadlock (HTTP 429 / `InsufficientPods`).
4. Restore the cluster to steady state safely.

---

## Prerequisites

- A Kubernetes cluster with at least **2 worker nodes** (so evicted pods have a schedulable destination).
- `kubectl` configured with cluster administrator access.

---

## Step 1: Deploy the Sample Workload

Apply the workload manifests using Kustomize:

```bash
kubectl apply -k kubernetes/workloads/sample-api
```

Wait until all 3 replicas are running and ready:

```bash
kubectl wait --namespace sample-workloads \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/name=sample-api \
  --timeout=60s
```

---

## Step 2: Verify Baseline Workload & PDB State

Inspect the Deployment and Pods:

```bash
kubectl get deployment sample-api -n sample-workloads
kubectl get pods -n sample-workloads -o wide
```

*Example Expected Output:*
```text
NAME         READY   UP-TO-DATE   AVAILABLE   AGE
sample-api   3/3     3            3           45s

NAME                          READY   STATUS    IP           NODE
sample-api-7b8f9c6d4b-2vkm8   1/1     Running   10.244.1.5   node-worker-1
sample-api-7b8f9c6d4b-9qx7p   1/1     Running   10.244.2.8   node-worker-2
sample-api-7b8f9c6d4b-lm4tz   1/1     Running   10.244.1.9   node-worker-1
```

Now inspect the PodDisruptionBudget:

```bash
kubectl get pdb sample-api -n sample-workloads -o wide
```

*Example Expected Output:*
```text
NAME         MIN AVAILABLE   MAX UNAVAILABLE   ALLOWED DISRUPTIONS   AGE   TOTAL
sample-api   2               N/A               1                     50s   3
```

Notice that `ALLOWED DISRUPTIONS` is **1**:
$$\text{Current Healthy (3)} - \text{Min Available (2)} = \text{Allowed Disruptions (1)}$$

---

## Step 3: Identify Target Node and Stream Events

Identify a worker node hosting at least one replica (e.g., `node-worker-2`):

In a separate terminal window, start streaming workload events:

```bash
kubectl get events -n sample-workloads --watch
```

---

## Step 4: Execute Controlled Node Drain

Drain the target node using the Eviction API:

```bash
kubectl drain node-worker-2 --ignore-daemonsets --delete-emptydir-data
```

*Example Expected Output:*
```text
node/node-worker-2 cordoned
WARNING: ignoring DaemonSet-managed Pods: kube-system/aws-node-xyz, kube-system/kube-proxy-abc
evicting pod sample-workloads/sample-api-7b8f9c6d4b-9qx7p
pod/sample-api-7b8f9c6d4b-9qx7p evicted
node/node-worker-2 drained
```

### What Happened Under the Hood:
1. `kubectl drain` cordoned `node-worker-2` (`SchedulingDisabled`).
2. `kubectl drain` called the Eviction API (`POST /api/v1/namespaces/sample-workloads/pods/sample-api-7b8f9c6d4b-9qx7p/eviction`).
3. The Eviction API evaluated the PDB:
   - `currentHealthy: 3` $\ge$ `minAvailable: 2` $\rightarrow$ Eviction permitted!
4. The evicted pod entered graceful termination (`SIGTERM`).
5. The Deployment controller detected a replica count deficit (2 active < 3 desired) and scheduled a replacement pod on an available node.
6. The replacement pod executed `startupProbe`, passed `readinessProbe`, and joined the active endpoints.

Inspect the PDB immediately after recovery:

```bash
kubectl get pdb sample-api -n sample-workloads
```

*Output:* `ALLOWED DISRUPTIONS` returned to **1**.

---

## Step 5: Uncordon the Worker Node

Return the drained node to service:

```bash
kubectl uncordon node-worker-2
```

---

## Step 6: Inject Failure Mode — Over-Restrictive PDB (`minAvailable: 3`)

Now test what happens when an operator or Helm chart misconfigures `minAvailable` to equal total replicas:

```bash
kubectl patch pdb sample-api -n sample-workloads \
  --type='merge' \
  -p '{"spec":{"minAvailable":3}}'
```

Inspect the PDB status:

```bash
kubectl get pdb sample-api -n sample-workloads
```

*Example Expected Output:*
```text
NAME         MIN AVAILABLE   MAX UNAVAILABLE   ALLOWED DISRUPTIONS   AGE
sample-api   3               N/A               0                     5m
```

Notice `ALLOWED DISRUPTIONS` is now **0**.

Query the detailed PDB conditions:

```bash
kubectl get pdb sample-api -n sample-workloads \
  -o jsonpath='{.status.conditions}' | python -m json.tool
```

*Example Expected Output:*
```json
[
    {
        "lastTransitionTime": "2026-09-04T12:00:00Z",
        "message": "The number of pods is insufficient to allow any disruptions.",
        "reason": "InsufficientPods",
        "status": "False",
        "type": "DisruptionAllowed"
    }
]
```

---

## Step 7: Attempt to Drain with Disruption Blocked

Now attempt to drain a node hosting one of the replicas:

```bash
kubectl drain node-worker-1 --ignore-daemonsets --delete-emptydir-data --timeout=30s
```

*Example Expected Output:*
```text
node/node-worker-1 cordoned
evicting pod sample-workloads/sample-api-7b8f9c6d4b-2vkm8
error when evicting pod "sample-api-7b8f9c6d4b-2vkm8" (will retry after 5s): Cannot evict pod as it would violate the pod's disruption budget.
evicting pod sample-workloads/sample-api-7b8f9c6d4b-2vkm8
error when evicting pod "sample-api-7b8f9c6d4b-2vkm8" (will retry after 5s): Cannot evict pod as it would violate the pod's disruption budget.
...
timed out waiting for the condition
```

### Observation:
The Eviction API returned **HTTP 429 Too Many Requests**. The node drain was halted, safely protecting the application's required serving floor, but also blocking host maintenance.

---

## Step 8: Safe Recovery & Cleanup

1. Uncordon the cordoned node:
   ```bash
   kubectl uncordon node-worker-1
   ```

2. Restore the calibrated PDB (`minAvailable: 2`):
   ```bash
   kubectl patch pdb sample-api -n sample-workloads \
     --type='merge' \
     -p '{"spec":{"minAvailable":2}}'
   ```

3. Confirm `ALLOWED DISRUPTIONS` returns to `1`:
   ```bash
   kubectl get pdb sample-api -n sample-workloads
   ```

4. If tearing down the lab:
   ```bash
   kubectl delete -k kubernetes/workloads/sample-api
   ```
