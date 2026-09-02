# Contributing to Production EKS Platform

Thank you for contributing to this platform engineering reference implementation. This repository evolves through reproducible implementations, verifiable evidence, and transparent engineering trade-offs.

---

## Workflow & Branching

We follow a structured Git feature branch workflow:

- **Target Branch:** All feature work branches from and targets `main`.
- **Branch Naming:** Use descriptive prefixes:
  - `feat/<milestone-or-topic>` (e.g., `feat/21-kubernetes-probes`)
  - `fix/<issue-description>`
  - `docs/<documentation-update>`
  - `refactor/<component>`
- **Direct Commits to Main:** Prohibited for feature additions. Only repository baseline bootstrapping occurs directly on `main`.

---

## Conventional Commits

Commits must follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

```text
<type>(<scope>): <short summary>

[optional body explaining context and trade-offs]
```

Allowed types:
- `feat`: A new platform capability, workload, or architecture component
- `fix`: A bug fix in manifests, code, or scripts
- `docs`: Documentation, ADRs, runbooks, or architecture diagrams
- `test`: Automated validations, test suites, or failure scenario reproductions
- `chore`: Maintenance, repository configuration, or dependency updates

---

## Architectural Decisions & Runbooks

To maintain the standard of "evidence over claims":

1. **Architecture Decision Records (ADRs):** Any significant architectural decision, trade-off, or structural choice must be documented as an ADR in `architecture/adr/` (following the ADR template).
2. **Failure Scenarios & Runbooks:** Operationally significant capabilities must be accompanied by:
   - A documented failure scenario in `failure-scenarios/` detailing how the system breaks.
   - An operational runbook in `runbooks/` following the `Symptom → Failure Domain → Evidence → Root Cause → Recovery` framework.

---

## Pre-Commit Standards & Hygiene

Before staging or pushing commits:

1. **No Secrets:** Ensure no credentials, API keys, private tokens, or real enterprise identities exist in your diff.
2. **Validate Syntax:** Run `./scripts/validate.sh` locally to confirm YAML validity, shell cleanliness, and repository hygiene.
3. **No Speculative Scaffolding:** Do not create empty directories or placeholder files for future technologies until the implementation is actively being built.
