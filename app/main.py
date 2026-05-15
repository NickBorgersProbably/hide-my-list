"""Entry point for the hide-my-list Python + LangGraph app.

When ENABLE_LANGGRAPH_PATH=false (default), the app prints "skeleton" and exits.
This keeps the new code dormant while OpenClaw continues running on main.

When ENABLE_LANGGRAPH_PATH=true, the app starts the Signal ingress listener
and APScheduler.

LangSmith guard: refuses to boot when LANGSMITH_TRACING=true unless
ALLOW_PRIVATE_TRACE_EXPORT=true is also set.
"""
from __future__ import annotations

import asyncio
import os
import sys

import structlog

log = structlog.get_logger()


def _check_langsmith_guard() -> None:
    """Refuse boot if LangSmith tracing is on without explicit private-export consent."""
    if os.environ.get("LANGSMITH_TRACING", "false").lower() == "true":
        if os.environ.get("ALLOW_PRIVATE_TRACE_EXPORT", "false").lower() != "true":
            print(  # noqa: T201
                "ERROR: LANGSMITH_TRACING=true but ALLOW_PRIVATE_TRACE_EXPORT is not set. "
                "Refusing to start: private conversation data must not be traced without "
                "explicit operator consent. Set ALLOW_PRIVATE_TRACE_EXPORT=true to override.",
                file=sys.stderr,
            )
            sys.exit(1)


async def _run_app() -> None:
    """Start Signal ingress and APScheduler when ENABLE_LANGGRAPH_PATH=true."""
    from app.graph.graph import build_graph, build_postgres_checkpointer
    from app.ingress.signal_listener import SignalListener
    from app.scheduler.jobs import reconcile_jobstore
    from app.scheduler.scheduler import build_scheduler
    from app.tools.db import get_connection_string, run_migrations

    log.info("app.starting", enable_langgraph_path=True)

    run_migrations()
    scheduler = await build_scheduler(skip_reconcile=True)
    scheduler.start(paused=True)
    reconcile_jobstore(scheduler)
    scheduler.resume()
    log.info("scheduler.started")

    database_url = get_connection_string()
    async with build_postgres_checkpointer(database_url) as checkpointer:
        graph = build_graph(checkpointer=checkpointer)
        listener = SignalListener(graph=graph)
        log.info("ingress.starting")
        await listener.run()


def main() -> None:
    _check_langsmith_guard()

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

    enable = os.environ.get("ENABLE_LANGGRAPH_PATH", "false").lower() == "true"

    if not enable:
        print("skeleton")  # noqa: T201
        sys.exit(0)

    asyncio.run(_run_app())


if __name__ == "__main__":
    main()
