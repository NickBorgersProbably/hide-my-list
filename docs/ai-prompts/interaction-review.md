# Post-Send Interaction Review

Assumes you've already read `docs/ai-prompts/shared.md` for the base prompt, shame-prevention templates, and output handling.

## Interaction Review

After a turn's reply is delivered, a second, slower pass re-reads the whole
turn and decides whether the conversation went where the user meant it to go.
It can repair one thing per turn and say so in one short follow-up message.

The review is not a graph node. It runs in the Signal listener as a background
task after the graph returns (`app/graph/interaction_review.py`), so it never
delays the initial reply. It yields to live conversation:

- It waits `INTERACTION_REVIEW_DELAY_SECONDS` (default 3) after the reply.
- It is skipped when the peer already has another message waiting.
- It is cancelled when the peer sends a new message before it starts acting.
  Once it has started acting, the next same-peer turn waits up to 60 seconds
  for it to finish, so that turn reads the corrected checkpoint.
- It is off when `INTERACTION_REVIEW_ENABLED=false`.

The model is the medium tier (caller `interaction_review`).

```mermaid
flowchart TD
    Sent([Reply delivered]) --> Wait[Wait a few seconds]
    Wait --> Pending{Newer message waiting?}
    Pending -->|Yes| Skip[Skip]
    Pending -->|No| Limit{Correction limit reached?}
    Limit -->|Yes| Skip
    Limit -->|No| Judge[Medium model reads the turn]
    Judge --> Valid{Verdict valid?}
    Valid -->|No| Store[Store verdict]
    Valid -->|ok| Store
    Valid -->|correct| Act[Run one action + send one follow-up]
    Act --> Store
```

### Inputs

The prompt carries:

- **The user's message** and **the reply that was delivered** this turn.
- **The classified intent** of the turn.
- **Conversation history** — the last 8 messages, 400 characters each,
  rendered like every other node prompt. It includes this turn.
- **Recent tasks** — the recent-task ledger, newest first, as rendered for
  other nodes.
- **What this turn did** — `turn_actions`, one line per action with its page id:
  `notion.create_task`, `notion.create_reminder`, `notion.update_status`
  (with the new status), `notion.update_property`, `reminder.cancel`,
  `suggest`, `reward`, `clarify`.
- **Open tasks** — every open Notion task and not-yet-fired reminder, as
  `{id, title, kind}`.
- **Completed this turn** — the pages this turn wrote `Completed` (a status
  change, or a page created already `Completed` when the user logs finished
  work), as `{id, title}`.
- **Current time** in UTC.

### Verdict Schema

The model returns exactly one JSON object and nothing else:

```json
{
  "verdict": "ok | correct",
  "reason": "one sentence explaining the verdict",
  "action": "none | complete_task | create_task | reopen_task | send_only",
  "page_id": "id from the lists above, or null",
  "title": "new task title for create_task, or null",
  "due": "ISO-8601 deadline for create_task, or null",
  "follow_up_message": "one short message containing {task}, or empty"
}
```

The application validates the verdict before acting and discards it on any
violation (logged as `interaction_review.verdict_rejected` with a rejection
code, stored with verdict `error`):

- Only the seven keys above; `verdict` and `action` from their enums.
- `ok` goes with action `none`, `page_id` null, and an empty follow-up.
  `correct` goes with any action except `none`.
- `complete_task`: `page_id` is one of the open task ids.
- `reopen_task`: `page_id` is one of the pages completed this turn, and the
  follow-up says the task is back on the list.
- `create_task`: `page_id` null, `title` non-empty, at most 200 characters,
  one line; `due` an ISO-8601 timestamp or null.
- `send_only`: `page_id` is an open id or a page completed this turn.
- Every corrective follow-up contains the literal `{task}` token, is at most
  400 characters, and carries no blame phrasing.

### Correction Policy

`ok` is the expected verdict for most turns. The review corrects only a clear
gap between what the user meant and what happened:

| Action | When | What the application does |
|---|---|---|
| `complete_task` | The user reported finishing something, the turn did not complete it, and exactly one open task plausibly matches | Writes Completed, cancels the reminder's pending outbox rows for a reminder page, clears its awaiting deliveries, runs the reward, and records `completed` in the ledger |
| `create_task` | The user clearly asked to track something and the turn saved nothing | Creates the task (with `due` when given) and records `added` |
| `reopen_task` | This turn completed a page the user did not report finishing | Writes Pending and records `added` |
| `send_only` | Nothing needs writing, but the reply left the user without an answer they asked for (for example, which task a question was about) | Sends the follow-up only |
| `none` | Everything else | Nothing |

Rules:

- One action per turn, and one follow-up message.
- The user's latest message decides. When they deferred, changed the subject,
  or said never mind, the verdict is `ok`.
- A question that is still waiting on the user's answer is not a gap: `ok`.
- When more than one task could match, the verdict is `ok` — the clarification
  already asked is the right move.
- The follow-up names the task through the `{task}` token and the draft's
  `notion_page_title`. The application substitutes the exact stored title
  (`render_task_token`) before sending; the model never writes the title
  itself.
- After a correction the application writes the checkpoint as the terminal
  `send` node: the follow-up joins `messages`, the ledger records the event,
  and any open clarification clears, so the next turn starts from the
  corrected state.

Limits: at most `INTERACTION_REVIEW_MAX_PER_HOUR` (default 3) executed
corrections per peer per hour; past that the review is skipped before the
model call. When executed corrections across all peers in 24 hours exceed
`INTERACTION_REVIEW_ALERT_THRESHOLD` (default 5), an ops alert of kind
`interaction_review_excess` goes to the operator. Every outcome — ok, correct,
skipped, error — is stored in the `interaction_reviews` table.

### Shame Prevention

The follow-up arrives unasked, seconds after the reply, so it must read as the
assistant tidying up its own work, never as a correction of the user:

- Say what happened, positively: "{task} — marked that one done." or
  "Added {task} to your list."
- Never point at a gap the user left — never use "you forgot", "you didn't", "you missed", or "you should have".
- Never contrast what the user said with the list ("that wasn't on your list").
- Never apologize at length or explain internals (no mention of reviews,
  models, Notion, or checks).
- One sentence, no question unless the action is `send_only` answering one.
