# Security

## What this is

hide-my-list is a personal AI agent that holds Notion API credentials and runs unattended on a VM. It's not a bank — but an always-on process with API keys deserves more thought than a typical side project. This doc describes what we actually do and why.

The infrastructure sections name specific technologies (Tailscale, Squid, UFW, Gravwell) because that's what we run. The principles matter more than the tools — deny-by-default egress, domain allowlisting, credential isolation, and network segmentation can be implemented with whatever fits your stack.

## Agent trust model

AI agents have a fundamental problem: prompt injection is unsolved. Any agent that processes untrusted input can potentially be manipulated. Meta's [Agents Rule of Two](https://ai.meta.com/blog/practical-ai-agent-security/) gives a useful framework for thinking about this. An agent becomes dangerous when it combines all three of:

- **[A]** Processes untrusted input
- **[B]** Accesses sensitive data or credentials
- **[C]** Changes state or communicates externally

The goal is to ensure no single session or component combines all three without a human in the loop. Here's how hide-my-list's components break down:

### Main agent — [BC] with a caveat

The agent's messaging channels are authenticated and paired — only the owner can send messages. No one else can reach the conversational interface. Administrative interfaces (SSH, control UI) additionally require Tailscale. So for direct user interaction, this is a **[BC]** configuration: sensitive data and state changes, but the input source is trusted.

The caveat is **GitHub content**. The agent can reach GitHub through the proxy allowlist, which means PR descriptions, issue bodies, and code content are potential prompt injection vectors. If the agent processes content from a repo that an attacker can contribute to, that's an untrusted input — making it effectively [ABC] for that interaction. This is a real risk. The proxy prevents the agent from stumbling into injections via arbitrary web browsing, but it can't filter what's inside allowlisted GitHub responses.

#### Credential exfiltration

The main credential theft risk isn't someone reading the config file off disk — admin surfaces are locked behind Tailscale and the file permissions are fine. The real risk is that the agent itself is an LLM that has access to its own API credentials at runtime and could be prompted into revealing them. This is the nature of the problem: you have a stochastic system that's helpful by design, sitting there ready to answer questions, and it has credentials in its context.

We use frontier-lab-hosted models with the expectation that their safety alignment provides some resistance to prompt injection. However, the research on this is not reassuring. Studies have found that more capable models can actually be *more* susceptible to prompt injection — the same instruction-following ability that makes them useful also makes them better at following injected instructions ([Li et al., EMNLP 2024](https://aclanthology.org/2024.emnlp-main.33/)). Some frontier models have shown better resistance through alignment work — Claude 3 resisted direct injection in [multimodal prompt injection testing](https://arxiv.org/html/2509.05883v1) where GPT-4o, Gemma, and LLaMA did not — but no model can reliably defend against prompt injection through alignment alone. We treat model-level resistance as a speed bump, not a wall.

#### Mitigations

Given that prompt injection is a when-not-if problem, the controls focus on limiting blast radius:

- **Proxy domain allowlist** limits where a manipulated agent can send data — even a successful injection can only reach allowlisted domains, not arbitrary endpoints
- **Proxy blocks private network ranges**, preventing a compromised agent from pivoting internally
- **Notion API scoping** — the integration token is scoped to specific databases, not the entire workspace
- **No destructive capabilities** — the agent can create and update tasks but has no access to delete data, send emails, or reach systems beyond Notion
- **Code execution** — the OpenClaw instance has arbitrary code execution (it can reconfigure itself); egress controls and Notion scoping limit what that execution can achieve

A prompt injection via GitHub content could still manipulate how the agent creates or labels tasks within Notion — the blast radius is limited by what the Notion token can do, not by preventing injection entirely. If the agent gained broader capabilities (more integrations, outbound messaging), the proxy allowlist and Notion scoping would need to be revisited — or we'd need human-in-the-loop confirmation for sensitive actions.

### Webhook — [A] only

The webhook listener ([`scripts/webhook-signal.sh`](scripts/webhook-signal.sh)) receives CI/CD notifications from the network **[A]**, but that's all it does. It immediately discards all request data (`exec 0</dev/null`) and writes a self-generated Unix timestamp to a signal file. It has no access to credentials **[B]** and makes no external calls **[C]**. There's nothing to exploit because nothing is read.

### CI/CD review agents — [AC] configuration

PR review agents process untrusted code **[A]** and write PR comments **[C]**, but have no access to infrastructure secrets or Notion credentials **[B]**. Workflows use only repo-scoped GitHub token permissions (contents/pull-requests/issues write for posting reviews); no infrastructure secrets or Notion credentials are available. This is a safe [AC] configuration — even if a malicious PR manipulated a review agent, there's no sensitive data to exfiltrate.

Additional CI/CD controls:
- Fork PRs are blocked from triggering Claude Code reviews (author must be a collaborator/member/owner)
- The devcontainer image is built only from the main branch, never from PR branches
- Review agents run on standard GitHub Actions runners with no infrastructure credentials

## Infrastructure controls

When the agent processes GitHub content it becomes [ABC], and infrastructure controls are the primary constraint on what a manipulated agent could do.

### Network

The VM runs on a VPS, not the home network, providing inherent network isolation from personal infrastructure.

The agent's conversational interface is reachable through OpenClaw's channels without Tailscale. Administrative interfaces (SSH, control UI) require Tailscale overlay network access with device posture checks.

On the host, UFW defaults to deny-all in both directions. Inbound allows only SSH and WireGuard. Outbound allows DNS, NTP, WireGuard, and traffic to the overlay subnet. HTTP/HTTPS ports are not open — all web traffic goes through a forward proxy.

### Forward proxy

A Squid proxy enforces a domain allowlist. If the agent (or anything else on the host) tries to reach a domain that isn't explicitly allowed, the connection is denied. This is the main backstop against data exfiltration — even if the agent is somehow manipulated, it can only talk to allowlisted domains.

**Caveat:** OpenClaw has code execution capability and could modify the local firewall or proxy rules. The on-host egress controls target prompt injection — only an injection success would attempt to remove them. This reduces risk rather than eliminating it.

The proxy also blocks connections to private network ranges (RFC 1918, loopback, link-local, and the overlay subnet itself) to prevent DNS rebinding attacks. Caching is disabled, `forwarded_for` headers are stripped, and the version string is suppressed.

### Application hardening

- Runs as a non-root user via systemd
- Config file (containing API keys) is `0600` — this protects against other local users reading it, but the real credential exposure risk is the agent itself being prompted into revealing them (see [credential exfiltration](#credential-exfiltration) above)
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
| Prompt injection via user message | Agent is [BC] for direct interaction — channels are authenticated/paired, only the owner can send messages | Low risk; owner is the only input source |
| Prompt injection via GitHub content | Becomes [ABC] when processing GitHub content — PR/issue bodies from external contributors are an injection vector | Blast radius limited to Notion operations the token permits; proxy limits exfiltration destinations |
| Agent pivots to internal network | [ABC] — an injected prompt could attempt lateral movement | Proxy blocks private ranges; firewall restricts egress to overlay subnet |
| Malicious webhook payload | Webhook is [A]-only — data discarded, no credentials or external access | Connection limits and hard timeout |
| Malicious PR manipulates review agent | Review agents are [AC] — no access to secrets or infrastructure | Fork PRs blocked; devcontainer built only from main |
| Credential exfiltration via prompt injection | The agent has credentials in its runtime context and could be prompted to reveal them | Proxy allowlist limits where credentials could be sent; admin surfaces behind Tailscale; model alignment is a speed bump, not a guarantee |
| Unauthorized admin access | Admin interfaces require Tailscale authentication | Firewall allows only SSH and WireGuard inbound |

## What would need to change

The agent is [BC] for direct interaction but becomes [ABC] when processing GitHub content. The current posture depends on infrastructure constraints (proxy allowlist, Notion scoping, VPS network isolation) to limit blast radius when that happens. The agent has arbitrary code execution via OpenClaw and could modify on-host controls, so egress restrictions reduce injection risk rather than eliminate it. Things that would require rethinking:

- **Broader API access** — currently limited to Notion; adding more integrations (calendar, email, file storage) widens what a compromised agent can do and would need per-integration scoping and possibly human-in-the-loop confirmation
- **Multi-user support** — one user's input could target another user's data; would need per-user credential scoping and session isolation
- **Outbound messaging** — if the agent could send emails or messages, a prompt injection could use it for spam or social engineering; would need human confirmation for outbound communication
- **Code execution escalation** — the agent already has arbitrary code execution via OpenClaw; adding access to more credentials or integrations would require sandboxing isolated from those secrets

## Reporting vulnerabilities

If you find a security issue, please report it through [GitHub's private vulnerability reporting](https://github.com/NickBorgersProbably/hide-my-list/security/advisories/new). Don't open a public issue.

We'll acknowledge your report within 48 hours.
