# Probe Failure Scenarios

This directory documents production failure modes centered on Kubernetes container lifecycle, health probe configuration, and traffic eligibility.

Each scenario breaks down the causal failure chain using our engineering framework:
$$\text{SYMPTOM} \longrightarrow \text{FAILURE DOMAIN} \longrightarrow \text{EVIDENCE} \longrightarrow \text{ROOT CAUSE} \longrightarrow \text{RECOVERY}$$

---

## Scenarios

- **[Scenario A: Premature Readiness (`Running \ne Ready`)](premature-readiness.md)**  
  *How a Pod in `Running` state triggers user-facing HTTP 5xx errors during rollouts due to misconfigured readiness signals.*
- **[Scenario B: Aggressive Liveness as an Outage Amplifier](aggressive-liveness.md)**  
  *How aggressive liveness checks turn transient local slowdowns, startup delays, or incorrectly coupled dependency failures into repeated restarts and reduced availability.*
