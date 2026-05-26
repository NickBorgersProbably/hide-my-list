# AGENTS.md — hide-my-list

This repo runs as a Python + LangGraph app. The runtime entry point is
`app/main.py`.

## Quick Start

```bash
docker compose up -d
docker compose logs -f app
```

For rollback instructions and the forward cutover procedure, see
`docs/python-rewrite/rollback.md`.

For contributor and CI agent context, see `DEV-AGENTS.md`.
