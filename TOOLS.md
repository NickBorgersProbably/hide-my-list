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

## State File

`state.json` — read on session start, update after state changes.
