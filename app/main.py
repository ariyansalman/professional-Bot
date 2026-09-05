"""Unified entrypoint.

``python -m app.main <service>`` where service is ``bot``, ``api``, ``worker``
or ``all``. Railway runs one service per process; ``all`` exists for local
development so a single command brings the whole platform up.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)

SERVICES = ("bot", "api", "worker", "all")


def run_api() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.api.app:app",
        host=settings.api.host,
        port=settings.api.port,
        log_config=None,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


async def _run_all() -> None:
    """Development convenience: bot + workers in one event loop."""
    import uvicorn

    from app.bot.main import build_bot, build_dispatcher
    from app.workers.base import run_workers
    from app.workers.main import build_workers

    settings = get_settings()
    bot = build_bot()
    dispatcher = build_dispatcher()

    config = uvicorn.Config(
        "app.api.app:app",
        host=settings.api.host,
        port=settings.api.port,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)

    await bot.delete_webhook(drop_pending_updates=False)
    tasks = [
        asyncio.create_task(server.serve(), name="api"),
        asyncio.create_task(
            dispatcher.start_polling(
                bot, allowed_updates=dispatcher.resolve_used_update_types()
            ),
            name="bot",
        ),
        asyncio.create_task(run_workers(build_workers()), name="workers"),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await bot.session.close()


def main() -> None:
    service = (sys.argv[1] if len(sys.argv) > 1 else get_settings().service).lower()
    if service not in SERVICES:
        raise SystemExit(f"unknown service {service!r}; choose one of {', '.join(SERVICES)}")

    settings = get_settings()
    configure_logging(settings.observability.level, settings.observability.json_output)
    log.info("process.starting", service=service, environment=settings.environment)

    if service == "bot":
        from app.bot.main import main as bot_main

        bot_main()
    elif service == "api":
        run_api()
    elif service == "worker":
        from app.workers.main import main as worker_main

        worker_main()
    else:
        with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
            asyncio.run(_run_all())


if __name__ == "__main__":
    main()
