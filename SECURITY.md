# Security

## What this is

hide-my-list is a personal AI agent that holds Notion API credentials and runs unattended on a VM. It's not a bank — but AI left unattended warrants some security consideration.

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

#### Why the GitHub tradeoff?

One feature which makes OpenClaw interesting is the fact it can change itself, and does so over time based on user feedback and its own memory. `hide-my-list` seeks to take advantage of this while also retaining external review of the behaviors [including the psych-review](https://github.com/NickBorgersProbably/hide-my-list/blob/main/.github/workflows/claude-code-review.yml#L1072-L1161). A belief held by the authors is that review from a second (or more) perspective(s) improves the quality of the output - so an OpenClaw instance alone with a single user-serving perspective should NOT be left in control of something like this. Instead, the OpenClaw instance is expected to take user feedback and craft Pull Requests against this repository so they can be more rigorously reviewed. After review, changes, and merge they get picked up as part of the system prompts.

GitHub has been chosen as the tool for facilitating this, and that means we need access for OpenClaw to open PRs and pull changes.

### Webhook — [A] only

The former webhook listener (`scripts/webhook-signal.sh`, now replaced by cron-based pipeline monitoring) received CI/CD notifications from the network **[A]**, but that was all it did. It immediately discarded all request data (`exec 0</dev/null`) and wrote a self-generated Unix timestamp to a signal file. It had no access to credentials **[B]** and made no external calls **[C]**. There was little to exploit because nothing was read.

### Reminder delivery flow — [BC] configuration

The reminder flow ([`scripts/check-reminders.sh`](scripts/check-reminders.sh) plus the OpenClaw `reminder-check` cron prompt) sources `.env` credentials **[B]**, queries Notion, and writes a local signal file **[C]**, but processes no untrusted input **[A]** — it only reads structured data from Notion that was created by the agent itself. This is a safe **[BC]** configuration.

### CI/CD review agents — [AC] configuration

PR review agents process untrusted code **[A]** and write PR comments **[C]**, but have no access to infrastructure secrets or Notion credentials **[B]**. Workflows use only repo-scoped GitHub token permissions (contents/pull-requests/issues write for posting reviews); no infrastructure secrets or Notion credentials are available. This is a safe [AC] configuration — even if a malicious PR manipulated a review agent, there's no sensitive data to exfiltrate.

Additional CI/CD controls:
- Fork PRs are blocked from triggering workflows on self-hosted runners (all workflows check `github.event.pull_request.head.repo.full_name == github.repository`; Claude Code reviews additionally require the author to be a collaborator/member/owner)
- The devcontainer image is built only from the main branch, never from PR branches
- Review agents run on self-hosted homelab runners with no infrastructure credentials; runners are isolated by the same VLAN segmentation and proxy controls as the main agent host
- Security gate jobs (`authorize`, `get-context`, `check-failure`, `get-pr-context`) remain on `ubuntu-latest` to process untrusted input before dispatching to self-hosted runners

### Prompt injection risk

The main risk isn't someone gaining access to the host — admin surfaces are locked behind Tailscale and the file permissions are fine. The real risk is that the agent itself is an LLM which could be prompted into acting for the adversary. This might include the credentials but really its about the data stored in Notion or other systems the agent has access to; credentials are merely how the data might be accessed.

We use frontier-lab-hosted models with the expectation that their safety alignment provides some resistance to prompt injection. However, the research on this is not reassuring. Studies have found that more capable models can actually be *more* susceptible to prompt injection — the same instruction-following ability that makes them useful also makes them better at following injected instructions ([Li et al., EMNLP 2024](https://aclanthology.org/2024.emnlp-main.33/)). Some frontier models have shown better resistance through alignment work — Claude 3 resisted direct injection in [multimodal prompt injection testing](https://arxiv.org/html/2509.05883v1) where GPT-4o, Gemma, and LLaMA did not — but no model can reliably defend against prompt injection through alignment alone. We treat model-level resistance as a speed bump, not a wall.

## Mitigations

When the agent processes GitHub content it becomes [ABC], and the mitigations below are the primary constraint on what a manipulated agent could do.

### Limited credentials

There are users of Openclaw giving it access to [their email](https://www.tomshardware.com/tech-industry/artificial-intelligence/openclaw-wipes-inbox-of-meta-ai-alignment-director-executive-finds-out-the-hard-way-how-spectacularly-efficient-ai-tool-is-at-maintaining-her-inbox), [their social media accounts](https://openclaws.io/blog/moltmatch-ai-dating/), or [their messaging apps](https://www.kaspersky.com/blog/openclaw-vulnerabilities-exposed/55263/). We are not those people.

Our OpenClaw instance gets credentials for:
- A Notion Page - literally a single notion page, not an entire account or workspace
- Some home systems related to home automation - read only access
- The GitHub repo for hide-my-list

### Accepted boundary

If prompt injection occurs, the entire OpenClaw host is considered compromised - and that's not one of our personal computers. The host does have limited access to our TailNet, but tagging is used to keep that limited to deliberate services which are themselves read-only. Another advantage of the GitHub oriented model is that the most valuable work of the system is outside the OpenClaw instance. We'll see if it happens, but it's possible we just nuke the VM and build another - after we cleanup the Notion DB credentials and recover what we can.

### Network isolation

The VM is a KVM virtual machine on the home network, not a personal laptop or shared system. VLAN segmentation isolates it from the internal network — the router blocks access to internal IP ranges from the OpenClaw VLAN.

The agent's conversational interface is reachable through OpenClaw's channels without Tailscale. Administrative interfaces (SSH, control UI) require Tailscale overlay network access with device posture checks.

On the host, UFW defaults to deny-all inbound with explicit allows for SSH and WireGuard only. Outbound is unrestricted at the host level — host-level services like Tailscale require unrestricted egress to function. OpenClaw runs in a Podman container; outbound HTTP/HTTPS from the container is routed through a forward proxy via the container's proxy environment variables. Kernel-level egress rules enforce this independently of the container's environment — even if the process unsets its proxy variables, direct internet traffic is dropped.

### Forward proxy

A Squid proxy enforces a domain allowlist. If the agent (or anything else on the host) tries to reach a domain that isn't explicitly allowed, the connection is denied. This provides mitigation against prompt injection because the agent will not go consuming arbitrary web content.

**Caveat:** OpenClaw has code execution capability and could attempt to reach destinations outside the proxy. Kernel-level egress rules (see [Network isolation](#network-isolation)) enforce this independently of the container's environment — modifying or unsetting proxy variables does not bypass the restriction.

The proxy also blocks connections to private network ranges (RFC 1918, loopback, link-local, and the overlay subnet itself) to prevent DNS rebinding attacks. Caching is disabled, `forwarded_for` headers are stripped, and the version string is suppressed.

### Webhook hardening

- Connections capped at 2 concurrent (`socat max-children=2`) with a 3-second hard timeout
- Exposed via Tailscale Funnel on a separate port from the control UI

### Configuration hardening

- Host is dedicated to the OpenClaw instance; no other services are hosted by the system
- Config file (containing API keys) is `0600` — this protects against other local users reading it, but the real credential exposure risk is the agent itself being prompted into revealing them (see [credential exfiltration](#credential-exfiltration) above)
- No credentials in the repo

## Threat model

| Threat | Trust model | Mitigation |
|--------|-------------|------------|
| Prompt injection via user message | Agent is [BC] for direct interaction — channels are authenticated/paired, only the owner can send messages | Low risk; owner is the only input source |
| Prompt injection via GitHub content | Becomes [ABC] when processing GitHub content — PR/issue bodies from external contributors are an injection vector | Blast radius limited to Notion operations the token permits; proxy limits exfiltration destinations |
| Agent pivots to internal network | [ABC] — an injected prompt could attempt lateral movement | Tailscale largely prevents access to internal systems; proxy blocks private ranges; VLAN segmentation blocks internal network access at the router level; kernel-level egress rules enforce restrictions independently of the container environment |
| Malicious webhook payload | Webhook is [A]-only — data discarded, no credentials or external access | Connection limits and hard timeout |
| Malicious PR manipulates review agent | Review agents are [AC] — no access to secrets or infrastructure | Fork PRs blocked from all self-hosted runner workflows; devcontainer built only from main; self-hosted runners isolated by VLAN segmentation |
| Credential exfiltration via prompt injection | The agent has credentials in its runtime context and could be prompted to reveal them | Proxy allowlist limits where credentials could be sent; admin surfaces behind Tailscale; model alignment is a speed bump, not a guarantee |
| Unauthorized admin access | Admin interfaces require Tailscale authentication and at least OpenClaw pairing for authentication | Firewall allows only SSH and WireGuard inbound |

## Reporting vulnerabilities

If you find a security issue, please report it through [GitHub's private vulnerability reporting](https://github.com/NickBorgersProbably/hide-my-list/security/advisories/new). Don't open a public issue.

We'll acknowledge your report within 48 hours.
