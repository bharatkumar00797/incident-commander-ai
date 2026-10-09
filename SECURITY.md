# Security Policy

Incident Commander is an AI agent that investigates production signals and proposes remediation,
so we take prompt-injection hardening, the remediation allowlist and API security seriously. The
design is described in the [security model](README.md#security-model) section of the README.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | yes |
| < 1.0 | no |

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

- Use GitHub's private vulnerability reporting:
  **Security → Report a vulnerability** on
  <https://github.com/bharatkumar00797/incident-commander-ai/security>.
- Include the affected version or commit, a description of the impact, and steps or a proof of
  concept to reproduce it.

You can expect an acknowledgement within 3 working days and a status update within 10 working
days. Once a fix is released we are happy to credit you in the changelog unless you prefer to
stay anonymous.

## Scope

In scope:

- Treating untrusted log / alert / metric text as instructions (prompt injection that escapes
  the `<untrusted-data>` wrapping or closes the block).
- Bypassing the diagnostic tool registry (arbitrary tools, shell, HTTP) or the runbook
  allowlist / param schema / preconditions.
- Executing a remediation without an approver-role decision, or forging the approval audit.
- Authentication or authorization bypass in the REST API: accessing another caller's incidents
  as a responder, approving with a responder-only key, using raw signals or a real provider in
  public-demo mode.
- Bypassing rate limits, the request body cap or the investigation queue bounds.
- Cross-site scripting or CSP bypass in the dashboard.
- Secrets exposed in API responses, logs or the published Docker image.

Out of scope:

- Incorrect root-cause conclusions or proposed runbooks from a real LLM provider; proposals
  always require human approval and run against the simulator in this project.
- Denial of service that requires more traffic than the configured rate limits allow.
- Shared public-demo ownership when `IC_TRUST_PROXY` is unset behind a reverse proxy
  (documented caveat: every visitor then shares one IP hash bucket).
- In-memory incident store loss across serverless instances (documented caveat).
- Vulnerabilities in third-party platforms (Render, Fly.io, Railway, Vercel) or dependencies
  without a demonstrated impact on Incident Commander.
