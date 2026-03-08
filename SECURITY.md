# Security

## What this is

hide-my-list is a personal AI agent that holds Notion API credentials and runs unattended on a VM. It's not a bank — but an always-on process with API keys deserves more thought than a typical side project. This doc describes what we actually do and why.

## Network access

The agent VM sits behind a Tailscale overlay network. There's no public IP and no open ports on the internet. You need to be on the Tailnet (with device posture checks) to reach anything.

On the host, UFW defaults to deny-all in both directions. Inbound allows only SSH and WireGuard. Outbound allows DNS, NTP, WireGuard, and traffic to the overlay subnet. HTTP/HTTPS ports are not open — all web traffic goes through a forward proxy.

### Forward proxy

A Squid proxy enforces a domain allowlist. If the agent (or anything else on the host) tries to reach a domain that isn't explicitly allowed, the connection is denied. This is the main control against data exfiltration — if the agent decides to phone home somewhere unexpected, it can't.

The proxy also blocks connections to private network ranges (RFC 1918, loopback, link-local, and the overlay subnet itself) to prevent DNS rebinding attacks from pivoting to internal services. Caching is disabled, `forwarded_for` headers are stripped, and the version string is suppressed.

All application and APT traffic is forced through the proxy via environment variables and apt config.

## Application

- Runs as a non-root user via systemd
- Config file (containing API keys) is `0600` — owner-only read/write
- No credentials in the repo
- Auto-restart with backoff (`RestartSec=10`) to avoid tight loops

## Webhook

The webhook listener ([`scripts/webhook-signal.sh`](scripts/webhook-signal.sh)) is deliberately minimal. It receives CI/CD notifications but immediately discards all request data (`exec 0</dev/null` before any processing). The only thing it does is write a Unix timestamp to a signal file. There's nothing to inject into because nothing is read.

Connections are capped at 2 concurrent (`socat max-children=2`) with a 3-second hard timeout. The webhook is exposed via Tailscale Funnel on a separate port from the control UI.

## CI/CD

- PR test workflows are read-only (`contents: read`, `pull-requests: read`) with no access to secrets
- Fork PRs are blocked from triggering Claude Code reviews — an authorization check verifies the PR author is a collaborator, member, or owner
- The devcontainer image is built only from the main branch, never from PR branches
- Review agents run on standard GitHub Actions runners with no infrastructure credentials

## Monitoring

- The proxy logs every request with domain, allow/deny decision, and client IP
- Logs are forwarded to a Gravwell SIEM instance
- Application output goes to the systemd journal

## Threat model

| Threat | Mitigation |
|--------|------------|
| Agent sends data to unauthorized endpoint | Proxy domain allowlist; firewall blocks direct egress |
| Agent pivots to internal network | Proxy blocks private ranges; firewall restricts egress to overlay subnet |
| Attacker sends malicious webhook payload | All request data is discarded before processing |
| API keys extracted from config or logs | Config is 0600; keys never logged or committed |
| Malicious PR triggers harmful code execution | Fork PRs blocked; devcontainer built only from main; workflows are read-only |
| Unauthorized access to agent UI or SSH | Tailscale overlay + device posture required; firewall allows only SSH and WireGuard inbound |
| Malicious dependency update | Proxy limits reachable package registries; CI runs in isolated containers |

## Reporting vulnerabilities

If you find a security issue, please report it through [GitHub's private vulnerability reporting](https://github.com/NickBorgersProbably/hide-my-list/security/advisories/new). Don't open a public issue.

We'll acknowledge your report within 48 hours.
