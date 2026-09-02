# Security Policy

## Public Reference Implementation

This repository is a **public reference implementation** for Kubernetes and platform engineering practices. Because it is intended for open review, education, and architecture demonstration, strict security boundaries apply to all contributions and stored artifacts.

---

## Prohibited Information

To prevent credential leakage and intellectual property exposure, **never commit real credentials or proprietary data**. Specifically prohibited:

- **Cloud Credentials:** AWS Access Keys (`AKIA...`, `ASIA...`), IAM secret keys, session tokens, or assume-role credentials.
- **Secrets & Passwords:** Database passwords, API tokens, bearer tokens, or webhook secrets.
- **Cryptographic Keys & Certificates:** Private keys (`.pem`, `.key`, `id_rsa`), x509 certificates with private key material, or PKCS bundles.
- **Proprietary Endpoints:** Internal enterprise DNS records, private VPC IP ranges, corporate VPN configurations, or proprietary service URLs.
- **Sensitive Data:** Customer data, production database dumps, or employer-specific infrastructure topologies.

All examples, manifests, and scripts in this repository must use **synthetic or sanitized values** (e.g., example domains like `example.internal`, placeholder hashes, or local mock services).

---

## Pre-Commit Verification

Before committing changes to this repository:

1. Verify that `.gitignore` actively excludes your local credential files (`.env`, `kubeconfig`, `*.pem`, `*.tfstate`).
2. Run local validation checks (such as `./scripts/validate.sh`) to scan for high-entropy tokens or common credential patterns.
3. Review staged diffs (`git diff --staged`) line by line before pushing to any branch.

---

## Reporting a Vulnerability

If you discover a security vulnerability, an accidental credential inclusion, or an insecure configuration pattern in this repository, please report it privately:

- **Contact:** Open a private security advisory through GitHub Security Advisories or contact the repository maintainer directly.
- Please do not open public GitHub issues for sensitive credential exposure.
