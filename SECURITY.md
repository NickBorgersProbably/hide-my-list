# Security

## What this is

hide-my-list is a personal AI agent that holds Notion API credentials and runs unattended on a VM. It's not a bank — but an always-on process with API keys deserves more thought than a typical side project. This doc describes what we actually do and why.

## Agent trust model

AI agents have a fundamental problem: prompt injection is unsolved. Any agent that processes untrusted input can potentially be manipulated. Meta's [Agents Rule of Two](https://ai.meta.com/blog/practical-ai-agent-security/) gives a useful framework for thinking about this. An agent becomes dangerous when it combines all three of:

- **[A]** Processes untrusted input
- **[B]** Accesses sensitive data or credentials
- **[C]** Changes state or communicates externally

The goal is to ensure no single session or component combines all three without a human in the loop. Here's how hide-my-list's components break down:

### Main agent — [ABC] configuration

The agent is reachable through OpenClaw's messaging channels, which are internet-facing — Tailscale only protects administrative interfaces like SSH and the control UI. That means the agent processes user input from outside the Tailnet **[A]**, accesses Notion data and API credentials **[B]**, and creates/updates tasks **[C]**. This is an [ABC] configuration, which the Rule of Two says requires additional controls.

The [A] surface breaks down into two categories:

- **User messages** — the owner could send a malicious prompt deliberately, but that's an accepted risk for a single-user system. The more realistic concern is pasting in content that contains an embedded injection.
- **GitHub content** — the agent can reach GitHub through the proxy allowlist, which means PR descriptions, issue bodies, and code content are all potential prompt injection vectors. This is a real and unmitigated risk. An attacker who can get content into a repo the agent reads could attempt injection.
- **Web content** — the proxy allowlist *does* help here. The agent can't fetch arbitrary URLs, so it's unlikely to stumble into prompt injections via web search. It can only reach explicitly allowlisted domains.

Our mitigations for the [ABC] configuration:
- **Proxy domain allowlist** limits where a manipulated agent can send data — even a successful prompt injection can only reach allowlisted domains, not arbitrary endpoints
- **Proxy blocks private network ranges**, preventing a compromised agent from pivoting internally
- **Notion API scoping** — the integration token is scoped to specific databases, not the entire workspace
- **No destructive capabilities** — the agent can create and update tasks but has no access to delete data, send emails, execute code, or reach systems beyond Notion

These are real mitigations but not complete ones. A prompt injection via GitHub content could still manipulate how the agent creates or labels tasks within Notion — the blast radius is limited by what the Notion token can do, not by preventing injection entirely. If the agent gained broader capabilities (more integrations, code execution, outbound messaging), the proxy allowlist and Notion scoping would need to be revisited — or we'd need human-in-the-loop confirmation for sensitive actions.

### Webhook — [A] only

The webhook listener ([`scripts/webhook-signal.sh`](scripts/webhook-signal.sh)) receives CI/CD notifications from the network **[A]**, but that's all it does. It immediately discards all request data (`exec 0</dev/null`) and writes a self-generated Unix timestamp to a signal file. It has no access to credentials **[B]** and makes no external calls **[C]**. There's nothing to exploit because nothing is read.

### CI/CD review agents — [AC] configuration

PR review agents process untrusted code **[A]** and write PR comments **[C]**, but have no access to infrastructure secrets or Notion credentials **[B]**. Workflows run with minimal permissions (`contents: read`, `pull-requests: read`). This is a safe [AC] configuration — even if a malicious PR manipulated a review agent, there's no sensitive data to exfiltrate.

Additional CI/CD controls:
- Fork PRs are blocked from triggering Claude Code reviews (author must be a collaborator/member/owner)
- The devcontainer image is built only from the main branch, never from PR branches
- Review agents run on standard GitHub Actions runners with no infrastructure credentials

## Infrastructure controls

Because the main agent is [ABC], infrastructure controls are not just defense-in-depth — they're the primary constraint on what a manipulated agent could do.

### Network

The agent's conversational interface is reachable through OpenClaw's channels without Tailscale. Administrative interfaces (SSH, control UI) require Tailscale overlay network access with device posture checks.

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

| Threat | Trust model | Mitigation |
|--------|-------------|------------|
| Prompt injection via user message | Agent is [ABC] — direct injection possible but single-user risk is accepted | Proxy allowlist limits exfiltration destinations; Notion token scoped to specific databases |
| Prompt injection via GitHub content | Agent is [ABC] — GitHub is allowlisted, so PR/issue content is an unmitigated injection vector | Blast radius limited to Notion operations the token permits; no destructive capabilities |
| Agent pivots to internal network | [ABC] — an injected prompt could attempt lateral movement | Proxy blocks private ranges; firewall restricts egress to overlay subnet |
| Malicious webhook payload | Webhook is [A]-only — data discarded, no credentials or external access | Connection limits and hard timeout |
| Malicious PR manipulates review agent | Review agents are [AC] — no access to secrets or infrastructure | Fork PRs blocked; devcontainer built only from main |
| API keys extracted from config | Keys only accessible to application user | Config is 0600; never logged or committed |
| Unauthorized admin access | Admin interfaces require Tailscale authentication | Firewall allows only SSH and WireGuard inbound |

## What would need to change

The agent is already [ABC], so the current posture depends on infrastructure constraints (proxy allowlist, Notion scoping, no destructive capabilities) to limit blast radius. Things that would require rethinking:

- **Broader API access** — currently limited to Notion; adding more integrations (calendar, email, file storage) widens what a compromised agent can do and would need per-integration scoping and possibly human-in-the-loop confirmation
- **Multi-user support** — one user's input could target another user's data; would need per-user credential scoping and session isolation
- **Outbound messaging** — if the agent could send emails or messages, a prompt injection could use it for spam or social engineering; would need human confirmation for outbound communication
- **Code execution** — any ability to run arbitrary code would require sandboxing isolated from the Notion credentials

## Reporting vulnerabilities

If you find a security issue, please report it through [GitHub's private vulnerability reporting](https://github.com/NickBorgersProbably/hide-my-list/security/advisories/new). Don't open a public issue.

We'll acknowledge your report within 48 hours.
