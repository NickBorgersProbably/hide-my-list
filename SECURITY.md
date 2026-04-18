# Security

## What this is

hide-my-list = personal AI agent with Notion API creds, runs unattended on VM. Not a bank — but unattended AI warrants security thought.

Infrastructure sections name specific tech (Tailscale, Squid, UFW, Gravwell) — that's what we run. Principles matter more: deny-by-default egress, domain allowlisting, credential isolation, network segmentation. Implement with whatever fits your stack.

## Agent trust model

AI agents have fundamental problem: prompt injection unsolved. Any agent processing untrusted input can be manipulated. Meta's [Agents Rule of Two](https://ai.meta.com/blog/practical-ai-agent-security/) gives useful framework. Agent becomes dangerous when combining all three:

- **[A]** Processes untrusted input
- **[B]** Accesses sensitive data or credentials
- **[C]** Changes state or communicates externally

Goal: no single session or component combines all three without human in loop. How hide-my-list components break down:

### Main agent — [BC] with a caveat

Agent messaging channels authenticated and paired — only owner can send messages. No one else reaches conversational interface. Admin interfaces (SSH, control UI) additionally require Tailscale. Direct user interaction = **[BC]**: sensitive data + state changes, trusted input source.

Caveat: **GitHub content**. Agent can reach GitHub through proxy allowlist — PR descriptions, issue bodies, code content are potential injection vectors. If agent processes content from repo an attacker can contribute to, that's untrusted input — effectively [ABC] for that interaction. Real risk. Proxy prevents agent stumbling into injections via arbitrary web browsing, but can't filter inside allowlisted GitHub responses.

#### Why the GitHub tradeoff?

One feature which makes OpenClaw interesting is the fact it can change itself, and does so over time based on user feedback and its own memory. `hide-my-list` seeks to take advantage of this while also retaining external review of the behaviors [including the psych-review](https://github.com/NickBorgersProbably/hide-my-list/blob/main/.github/workflows/codex-code-review.yml#L1072-L1161). Belief held by authors: review from second (or more) perspective(s) improves output quality — OpenClaw instance alone with single user-serving perspective should NOT control something like this. Instead, OpenClaw instance expected to take user feedback and craft Pull Requests against this repo for rigorous review. After review, changes, merge — picked up as part of system prompts.

GitHub chosen to facilitate this. Means OpenClaw needs access to open PRs and pull changes.

### Cron-driven reminder flow — [BC] configuration

Reminder flow ([`scripts/check-reminders.sh`](scripts/check-reminders.sh) plus OpenClaw `reminder-check` durable cron job) keeps shell narrow, lets `scripts/notion-cli.sh` load just Notion credentials **[B]**, writes reminder handoff file in repo root (default: `.reminder-signal`) **[C]**. Processes no untrusted input **[A]** — reads only structured Notion data created by agent itself. Safe **[BC]** configuration.


### CI/CD review agents — [AC] configuration

PR review agents process untrusted code **[A]** and write PR comments **[C]**, no access to infrastructure secrets or Notion credentials **[B]**. Workflows use only repo-scoped GitHub token permissions (contents/pull-requests/issues write for posting reviews). Safe [AC] — even if malicious PR manipulates review agent, no sensitive data to exfiltrate.

Additional CI/CD controls:
- Fork PRs blocked from triggering workflows on self-hosted runners (all workflows check `github.event.pull_request.head.repo.full_name == github.repository`; Codex Code Review workflows additionally require author to be collaborator/member/owner)
- Devcontainer image built only from main branch, never from PR branches
- Review agents run on self-hosted homelab runners with no infrastructure credentials; runners isolated by same VLAN segmentation and proxy controls as main agent host
- Security gate jobs (`authorize`, `get-context`, `check-failure`, `get-pr-context`) stay on `ubuntu-latest` to process untrusted input before dispatching to self-hosted runners

### Prompt injection risk

Main risk isn't host access — admin surfaces locked behind Tailscale, file permissions fine. Real risk: agent is an LLM that could be prompted into acting for adversary. May include credentials, but really about data in Notion or other systems agent accesses; credentials are just how data gets reached.

We use frontier-lab-hosted models expecting their safety alignment resists prompt injection. Research not reassuring. Studies show more capable models can be *more* susceptible — same instruction-following ability that makes them useful makes them better at following injected instructions ([Li et al., EMNLP 2024](https://aclanthology.org/2024.emnlp-main.33/)). Some frontier models show better resistance via alignment — Claude 3 resisted direct injection in [multimodal prompt injection testing](https://arxiv.org/html/2509.05883v1) where GPT-4o, Gemma, LLaMA did not — but no model reliably defends via alignment alone. Model-level resistance = speed bump, not wall.

## Mitigations

Mitigations below constrain what manipulated agent could do.

### Limited credentials

Users of Openclaw give it access to [their email](https://www.tomshardware.com/tech-industry/artificial-intelligence/openclaw-wipes-inbox-of-meta-ai-alignment-director-executive-finds-out-the-hard-way-how-spectacularly-efficient-ai-tool-is-at-maintaining-her-inbox), [their social media](https://openclaws.io/blog/moltmatch-ai-dating/), or [their messaging apps](https://www.kaspersky.com/blog/openclaw-vulnerabilities-exposed/55263/). We are not those people.

Our OpenClaw instance gets credentials for:
- A Notion Page - literally single notion page, not entire account or workspace
- Some home systems for home automation - read only access
- The GitHub repo for hide-my-list

### Accepted boundary

If prompt injection occurs, entire OpenClaw host considered compromised — not one of our personal computers. Host has limited TailNet access, but tagging keeps that limited to deliberate services, themselves read-only. Another advantage of GitHub-oriented model: most valuable work lives outside OpenClaw instance. If it happens, we nuke the VM and build another — after cleaning Notion DB credentials and recovering what we can.

### Network isolation

VM is KVM virtual machine on home network, not personal laptop or shared system. VLAN segmentation isolates from internal network — router blocks internal IP ranges from OpenClaw VLAN.

Agent conversational interface reachable through OpenClaw's channels without Tailscale. Admin interfaces (SSH, control UI) require Tailscale overlay with device posture checks.

On host, UFW defaults deny-all inbound with explicit allows for SSH and WireGuard only. Outbound unrestricted at host level — host-level services like Tailscale require unrestricted egress. OpenClaw runs in Podman container; outbound HTTP/HTTPS from container routed through forward proxy via container's proxy environment variables. Kernel-level egress rules enforce this independently of container environment — even if process unsets proxy variables, direct internet traffic dropped.

### Forward proxy

Squid proxy enforces domain allowlist. Agent (or anything on host) trying to reach non-allowlisted domain — connection denied. Mitigates prompt injection: agent won't consume arbitrary web content.

**Caveat:** OpenClaw has code execution capability and could attempt reaching destinations outside proxy. Kernel-level egress rules (see [Network isolation](#network-isolation)) enforce this independently of container environment — modifying or unsetting proxy variables does not bypass restriction.

Proxy also blocks connections to private network ranges (RFC 1918, loopback, link-local, overlay subnet) to prevent DNS rebinding. Caching disabled, `forwarded_for` headers stripped, version string suppressed.

### Inbound exposure reduction

- Old `socat`-based webhook listener removed; routine operations rely on durable cron, not required inbound listener
- Durable cron polling (`reminder-check`, `pull-main`) covers routine operation, survives agent restarts via OpenClaw's cron subsystem
- Heartbeat re-registers cron jobs if they disappear, ensuring continuity for cron path
- Optional GitHub-triggered webhook notifications may still exist if operator configures `AGENT_WEBHOOK_URL` — inbound exposure reduced but not universally eliminated

### Configuration hardening

- Host dedicated to OpenClaw instance; no other services hosted
- Config file (containing API keys) is `0600` — protects against other local users reading it, but real credential exposure risk is agent itself being prompted to reveal them (see [credential exfiltration](#credential-exfiltration) above)
- No credentials in repo

## Threat model

| Threat | Trust model | Mitigation |
|--------|-------------|------------|
| Prompt injection via user message | Agent is [BC] for direct interaction — channels authenticated/paired, only owner can send messages | Low risk; owner is only input source |
| Prompt injection via GitHub content | Becomes [ABC] when processing GitHub content — PR/issue bodies from external contributors are injection vector | Blast radius limited to Notion operations token permits; proxy limits exfiltration destinations |
| Agent pivots to internal network | [ABC] — injected prompt could attempt lateral movement | Tailscale largely prevents access to internal systems; proxy blocks private ranges; VLAN segmentation blocks internal network at router level; kernel-level egress rules enforce restrictions independently of container environment |
| Malicious webhook payload | Reduced but not eliminated — cron is primary path, but optional workflow notifications may still post to agent webhook | Old `socat` listener gone; core operation relies on cron polling; any configured `AGENT_WEBHOOK_URL` path should be treated as additional inbound surface |
| Malicious PR manipulates review agent | Review agents are [AC] — no access to secrets or infrastructure | Fork PRs blocked from all self-hosted runner workflows; devcontainer built only from main; self-hosted runners isolated by VLAN segmentation |
| Credential exfiltration via prompt injection | Agent has credentials in runtime context, could be prompted to reveal them | Proxy allowlist limits where credentials could be sent; admin surfaces behind Tailscale; model alignment is speed bump, not guarantee |
| Unauthorized admin access | Admin interfaces require Tailscale authentication and at least OpenClaw pairing | Firewall allows only SSH and WireGuard inbound |

## Reporting vulnerabilities

Security issue found — report via [GitHub's private vulnerability reporting](https://github.com/NickBorgersProbably/hide-my-list/security/advisories/new). No public issues.

Acknowledge within 48 hours.