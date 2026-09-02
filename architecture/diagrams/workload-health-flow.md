# Workload Health & Traffic Flow Architecture

This diagram illustrates the decoupling between the **Kubernetes Node Control Plane (Health Signals)** and the **Runtime Data Plane (User Traffic Path)**.

A foundational reliability principle in Kubernetes is:
$$\text{Container Running} \ne \text{Application Ready}$$

---

## Architecture Flow Diagram

```mermaid
flowchart TD
    subgraph DATA_PLANE["Runtime Data Plane (User Request Path)"]
        direction TB
        Client["External Client / Consumer"]
        Ingress["External traffic layer (implementation-dependent)\nIngress / Gateway API / Cloud Load Balancer"]
        K8sService["Kubernetes Service (ClusterIP)"]
        EndpointSlice["EndpointSlice Controller\n(Filters by Pod Ready Condition)"]
        
        Client -->|"HTTP Request"| Ingress
        Ingress -->|"Route Match"| K8sService
        K8sService -.->|"Queries Active Slices"| EndpointSlice
        EndpointSlice -->|"Only forwards if\nReady == True"| PodContainer
    end

    subgraph CONTROL_PLANE["Node Control Plane (Kubelet Health Evaluation)"]
        direction TB
        Kubelet["Node Kubelet Agent"]
        
        subgraph PROBES["Probe Execution Boundary"]
            Startup["startupProbe (/startup)\nGuards cold start"]
            Readiness["readinessProbe (/ready)\nEvaluates serving capacity"]
            Liveness["livenessProbe (/health)\nEvaluates process responsiveness"]
        end
        
        Kubelet -->|"1. Polls until OK"| Startup
        Startup -->|"Once passed,\nactivates"| Readiness
        Startup -->|"Once passed,\nactivates"| Liveness
        
        Readiness -->|"Updates Condition:\nPodReady = True/False"| PodStatus["Pod Status / Conditions\n(Control Plane State)"]
        Liveness -->|"Failure Threshold Exceeded"| RestartAction["Trigger Container Restart\n(kills process via SIGTERM/SIGKILL)"]
    end

    subgraph WORKLOAD_POD["Kubernetes Workload Pod"]
        direction TB
        PodContainer["Pod Network & Container Namespace"]
        AppRuntime["Application Process (sample-api)\nRunning in Container"]
        
        PodContainer --> AppRuntime
        PodStatus -.->|"Synchronizes state to API Server"| EndpointSlice
        RestartAction -.->|"Re-executes container"| PodContainer
    end

    %% Probes querying application
    Startup -.->|"HTTP GET /startup"| AppRuntime
    Readiness -.->|"HTTP GET /ready"| AppRuntime
    Liveness -.->|"HTTP GET /health"| AppRuntime

    %% Styling
    classDef control fill:#1e293b,stroke:#38bdf8,stroke-width:2px,color:#f8fafc;
    classDef data fill:#0f172a,stroke:#34d399,stroke-width:2px,color:#f8fafc;
    classDef workload fill:#312e81,stroke:#818cf8,stroke-width:2px,color:#f8fafc;
    classDef alert fill:#7f1d1d,stroke:#f87171,stroke-width:2px,color:#fef2f2;

    class Kubelet,Startup,Readiness,Liveness,PodStatus control;
    class Client,Ingress,K8sService,EndpointSlice data;
    class PodContainer,AppRuntime workload;
    class RestartAction alert;
```

---

## Architectural Distinctions

### 1. Control Plane Signal vs. Runtime Request Path
- **Kubelet is completely isolated from user traffic.** The kubelet acts strictly as an administrative node agent executing health probes on configured intervals via loopback or container network interfaces.
- **The Data Plane relies on EndpointSlice readiness.** Ingress, load balancers, and kube-proxy/eBPF routing rules only forward client traffic to Pod IPs that have the condition `Ready = True`.

### 2. Failure Domain Isolation
| Failure Condition | Evaluated By | Action Taken | Traffic Impact | Container Impact |
| :--- | :--- | :--- | :--- | :--- |
| **Cold start / cache warm-up** | `startupProbe` | Holds off liveness and readiness evaluation | Pod is not yet in EndpointSlice; no traffic received | Process continues initializing undisturbed |
| **Downstream DB outage / Queue full** | `readinessProbe` | Sets `PodReady = False` | Removed from EndpointSlice; 0 incoming requests | **No restart**; preserves memory & prevents restart storm |
| **Application deadlock / fatal hang** | `livenessProbe` | Emits `Unhealthy` event to kubelet | Removed from endpoints during restart | Kubelet terminates and restarts container |

> [!NOTE]
> Specific data plane components (Ingress controllers, AWS ALB Controller, Cilium eBPF, kube-proxy, Service Meshes) route traffic using different lower-level mechanics, but all standard implementations honor the Kubernetes `PodReady` condition synchronized via the EndpointSlice API.
