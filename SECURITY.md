# Security Policy

## Supported Versions

Security updates are provided for the latest release of the TUVL engine. Please ensure you stay up to date.

| Version | Supported          |
| ------- | ------------------ |
| 1.x     | :white_check_mark: |
| 2026.x (pre-1.0 test builds) | :x: |

## Reporting a Vulnerability

We take the security of the TUVL framework seriously. If you believe you have found a security vulnerability, **please do not report it via a public GitHub issue**.

Instead, please report it privately by emailing [security@tuvl.io](mailto:security@tuvl.io).

Please provide a clear and concise description of the issue, along with steps to reproduce it. We will acknowledge receipt of your vulnerability report within 48 hours and strive to send you regular updates about our progress.

Machine-readable contact details are published at
[`https://tuvl.io/.well-known/security.txt`](https://tuvl.io/.well-known/security.txt) (RFC 9116).

### What to include in your report:
- TUVL version(s) affected
- A detailed description of the vulnerability and its potential impact
- Steps to reproduce the issue
- Any potential mitigations you've identified

### Encrypted reports

If your report contains sensitive details (for example a working exploit), you
can encrypt it to our OpenPGP key, published at
[`https://tuvl.io/.well-known/security-pgp.asc`](https://tuvl.io/.well-known/security-pgp.asc)
(also referenced from `security.txt`). Verify the fingerprint before use:

```
BA7B 4C3D A0C3 3120 113D EECB 9C9F 1F4E A206 4503
```

### Safe harbor

We consider security research and vulnerability disclosure conducted in good
faith under this policy to be authorized, and we will not pursue or support
legal action against you for it. "Good faith" means: you make a reasonable
effort to avoid privacy violations, data destruction, and service disruption;
you only access the minimum data needed to demonstrate the issue; and you give
us a reasonable opportunity to remediate before disclosing publicly. If in
doubt about whether a specific action is authorized, ask us first at
security@tuvl.io.

### Reporting channels

Email is the primary channel today. The tuvl engine source is currently
developed privately and will be published in a future release; once the code is
public we will also enable **GitHub Private Vulnerability Reporting** on the
public repository and list it here as an alternative.

Thank you for helping keep TUVL secure!
