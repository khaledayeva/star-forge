# Security Policy

## Supported Versions

Star Forge is currently in active development. Security fixes target the latest
published release and the `main` branch.

## Reporting A Vulnerability

Please use GitHub's private vulnerability reporting flow under the repository's
Security tab. Do not open a public issue for an unpatched vulnerability.

Include enough detail to reproduce and assess the problem:

- affected version or commit
- relevant command or collector
- expected and observed behavior
- a minimal reproduction when possible
- whether credentials, local files, generated evidence, or remote systems are at
  risk

Remove real credentials and personal data from reports and attachments. Use
synthetic fixtures whenever they can demonstrate the issue safely.

You can expect an initial acknowledgment after the report is reviewed. A fix and
disclosure timeline will depend on severity, exploitability, and release scope.

## Security Boundaries

Star Forge records local project evidence and may run task verification commands
declared in `Plan.md`. Review those commands before using Star Forge on an
untrusted repository.

Live collectors are designed to fail closed when evidence is malformed, stale,
fixture-derived, source-mismatched, or outside the task-scoped live directory.
Observer hooks are diagnostic and do not represent a trusted host-controlled
witness in the current release.
