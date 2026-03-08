# Security

## What this is

hide-my-list is a personal AI agent that holds Notion API credentials and runs unattended on a VM. It's not a bank — but an always-on process with API keys deserves more thought than a typical side project. This doc describes what we actually do and why.

## Agent trust model

AI agents have a fundamental problem: prompt injection is unsolved. Any agent that processes untrusted input can potentially be manipulated. Meta's [Agents Rule of Two](https://ai.meta.com/blog/practical-ai-agent-security/) gives a useful framework for thinking about this. An agent becomes dangerous when it combines all three of:

- **[A]** Processes untrusted input
- **[B]** Accesses sensitive data or credentials
- **[C]** Changes state or communicates externally

The goal is to ensure no single session or component combines all three without a human in the loop. Here's how hide-my-list's components break down:

### Main agent — [BC] configuration

The agent accesses Notion data **[B]** and writes tasks **[C]**, but its input channel is constrained. Only an authenticated user behind Tailscale can talk to it — there's no public API, no open web interface, no email ingestion. User input is treated as trusted because the network layer ensures only the owner can provide it. This makes it a **[BC]** configuration: sensitive data and state changes, but no untrusted input.

If we ever add input channels that aren't user-initiated (email ingestion, Slack integration, processing web content), this stops being [BC] and we'd need to add controls — either sandboxing the untrusted input processing away from Notion access, or requiring human confirmation before the agent acts on external data.

### Webhook — [A] only

The webhook listener ([`scripts/webhook-signal.sh`](scripts/webhook-signal.sh)) receives CI/CD notifications from the network **[A]**, but that's all it does. It immediately discards all request data (`exec 0</dev/null`) and writes a self-generated Unix timestamp to a signal file. It has no access to credentials **[B]** and makes no external calls **[C]**. There's nothing to exploit because nothing is read.

### CI/CD review agents — [AC] configuration

PR review agents process untrusted code **[A]** and write PR comments **[C]**, but have no access to infrastructure secrets or Notion credentials **[B]**. Workflows run with minimal permissions (`contents: read`, `pull-requests: read`). This is a safe [AC] configuration — even if a malicious PR manipulated a review agent, there's no sensitive data to exfiltrate.

Additional CI/CD controls:
- Fork PRs are blocked from triggering Claude Code reviews (author must be a collaborator/member/owner)
- The devcontainer image is built only from the main branch, never from PR branches
- Review agents run on standard GitHub Actions runners with no infrastructure credentials

## Infrastructure controls

The trust model above describes *what* the agent can do. The infrastructure below constrains *how* it can do it — defense in depth in case the application-level assumptions are wrong.

### Network

The agent VM sits behind a Tailscale overlay network. There's no public IP and no open ports on the internet. You need to be on the Tailnet (with device posture checks) to reach anything.

On the host, UFW defaults to deny-all in both directions. Inbound allows only SSH and WireGuard. Outbound allows DNS, NTP, WireGuard, and traffic to the overlay subnet. HTTP/HTTPS ports are not open — all web traffic goes through a forward proxy.

### Forward proxy

A Squid proxy enforces a domain allowlist. If the agent (or anything else on the host) tries to reach a domain that isn't explicitly allowed, the connection is denied. This is the main backstop against data exfiltration — even if the agent is somehow manipulated, it can only talk to allowlisted domains.

The proxy also blocks connections to private network ranges (RFC 1918, loopback, link-local, and the overlay subnet itself) to prevent DNS rebinding attacks. Caching is disabled, `forwarded_for` headers are stripped, and the version string is suppressed.

### Application hardening

- Runs as a non-root user via systemd
- Config file (containing API keys) is `0600` — owner-only read/write
- No credentials in the repo
- Auto-restart with backoff (`RestartSec=10`) to avoid tight loops

### Webhook hardening

- Connections capped at 2 concurrent (`socat max-children=2`) with a 3-second hard timeout
- Exposed via Tailscale Funnel on a separate port from the control UI

## Monitoring

- The proxy logs every request with domain, allow/deny decision, and client IP
- Logs are forwarded to a Gravwell SIEM instance
- Application output goes to the systemd journal

## Threat model

| Threat | Trust model | Infrastructure backstop |
|--------|-------------|------------------------|
| Prompt injection causes data exfiltration | Agent is [BC] — no untrusted input channel to inject through | Proxy allowlist blocks unauthorized destinations |
| Agent pivots to internal network | [BC] config has no external input to trigger pivoting | Proxy blocks private ranges; firewall restricts egress |
| Malicious webhook payload | Webhook is [A]-only — all data discarded, no access to credentials or external systems | Connection limits and hard timeout |
| Malicious PR manipulates review agent | Review agents are [AC] — no access to secrets or infrastructure | Fork PRs blocked; devcontainer built only from main |
| API keys extracted from config | Keys are only accessible to the application user | Config is 0600; never logged or committed |
| Unauthorized access to agent | Input channel requires Tailscale authentication | Firewall allows only SSH and WireGuard inbound |

## What would need to change

The current security posture assumes a single trusted user as the only input source. Things that would require rethinking:

- **Adding untrusted input channels** (email, Slack, web content) — the main agent would become [ABC], requiring either human-in-the-loop confirmation for actions, or sandboxing the input processing away from Notion access
- **Multi-user support** — one user's input could target another user's data; would need per-user credential scoping
- **Broader API access** — currently limited to Notion; adding more integrations increases what a compromised agent can do

## Reporting vulnerabilities

If you find a security issue, please report it through [GitHub's private vulnerability reporting](https://github.com/NickBorgersProbably/hide-my-list/security/advisories/new). Don't open a public issue.

We'll acknowledge your report within 48 hours.
