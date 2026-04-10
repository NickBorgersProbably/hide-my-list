# TOOLS.md — hide-my-list Local Notes

## Notion

- **API Key**: In `.env`
- **Database ID**: In `.env`
- **CLI**: `scripts/notion-cli.sh`
- **Schema**: See `docs/notion-schema.md`

### Notion Property Names (must match exactly)
- Title, Status, Work Type, Urgency, Time Estimate (min)
- Energy Required, Rejection Count, Inline Steps
- Parent Task, Sequence, Steps Completed, Resume Count
- Completed At, Started At, Is Reminder, Remind At, Reminder Status

### Status Values
- Pending, In Progress, Completed, Has Subtasks

### Work Type Values
- Focus, Creative, Social, Independent

### Energy Values
- High, Medium, Low

### Reminder Status Values
- pending, sent, missed

## Message Tool

Use the OpenClaw `message` tool for proactive outbound delivery that is not part of a normal assistant reply.

### Reminder Delivery Contract
- `action: send`
- `channel: signal`
- `target: channels.signal.defaultTo` from `openclaw.json`
- message body: the reminder text to deliver

The `message` tool is available in both the main session startup check and heartbeat sessions. Reminder delivery counts as successful only when the tool call succeeds. If `channels.signal.defaultTo` is missing or malformed, skip `complete-reminder` and leave the handoff file in place for retry after config is fixed.

## State File

`state.json` — read on session start, update after state changes.
