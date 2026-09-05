"""Worker process entrypoint.

Runs every background worker in one process by default. Individual workers can
be selected with ``--only`` so a deployment can scale them independently, for
example running verification on its own replica.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis
from app.db.session import dispose_engine
from app.workers.base import PeriodicWorker, run_workers

log = get_logger(__name__)


def build_workers(names: set[str] | None = None) -> list[PeriodicWorker]:
    """Instantiate the requested workers.

    Workers that need to message customers get a Bot instance; the rest do not,
    so a worker-only deployment without a bot token still runs the financial
    loops.
    """
    from app.workers.delivery.dispatcher import DeliveryWorker
    from app.workers.notifications.dispatcher import (
        BroadcastWorker,
        NotificationWorker,
        RestockNotifierWorker,
    )
    from app.workers.payment.verification import (
        PaymentExpiryWorker,
        PaymentVerificationWorker,
        ProviderHealthWorker,
        ReservationReaperWorker,
    )
    from app.workers.reconciliation.service import (
        IdempotencyCleanupWorker,
        ReconciliationWorker,
    )
    from app.workers.webhooks.dispatcher import WebhookWorker

    bot = None
    if get_settings().telegram.bot_token.get_secret_value():
        from app.bot.main import build_bot

        bot = build_bot()

    candidates: list[PeriodicWorker] = [
        PaymentVerificationWorker(bot),
        PaymentExpiryWorker(bot),
        ReservationReaperWorker(),
        DeliveryWorker(bot),
        WebhookWorker(),
        ReconciliationWorker(),
        ProviderHealthWorker(),
        IdempotencyCleanupWorker(),
    ]
    if bot is not None:
        candidates += [
            NotificationWorker(bot),
            BroadcastWorker(bot),
            RestockNotifierWorker(bot),
        ]

    if names:
        selected = [worker for worker in candidates if worker.name in names]
        unknown = names - {worker.name for worker in candidates}
        if unknown:
            raise SystemExit(f"unknown worker(s): {', '.join(sorted(unknown))}")
        return selected
    return candidates


async def _run(names: set[str] | None) -> None:
    settings = get_settings()
    configure_logging(settings.observability.level, settings.observability.json_output)

    workers = build_workers(names)
    log.info(
        "workers.starting",
        service="worker",
        workers=[worker.name for worker in workers],
        environment=settings.environment,
    )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _request_stop() -> None:
        log.info("workers.shutdown_requested")
        for worker in workers:
            worker.stop()
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)

    try:
        await run_workers(workers)
    finally:
        await dispose_engine()
        await close_redis()
        log.info("workers.stopped")


def main(argv: list[str] | None = None) -> None:
    """Entrypoint.

    ``argv`` is passed explicitly by ``app.main`` so the dispatching service
    name is not re-parsed here; it defaults to ``sys.argv[1:]`` when the module
    is run directly.
    """
    parser = argparse.ArgumentParser(prog="app.main worker", description="Background workers")
    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        help="Run only the named workers (default: all)",
    )
    args = parser.parse_args(argv)
    names = set(args.only) if args.only else None
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(_run(names))


if __name__ == "__main__":
    main()
