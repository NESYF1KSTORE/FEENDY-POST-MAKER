"""Worker loop: leases jobs, runs handlers, publishes events.

Each job runs in its own transaction. A handler that raises leaves no partial
state behind — the transaction rolls back and only the retry bookkeeping is
committed, which is what makes at-least-once execution safe.
"""

from __future__ import annotations

import os
import signal
import socket
import time
import uuid

from app.config import get_settings
from app.core.errors import (
    ApprovalRequired,
    BudgetExceeded,
    GateBlocked,
    PermissionDenied,
    PolicyConflict,
    ProviderError,
    ValidationError,
)
from app.core.logging import bind_context, clear_context, configure, get_logger, set_correlation_id
from app.db import session_scope
from app.models.execution import Job
from app.orchestrator import events, handlers, jobs

log = get_logger("fynix.worker")

# Handlers are registered by importing the module; keep the reference explicit
# so linters do not drop the import.
_ = handlers

#: Failures that will never succeed on retry — fail the job immediately rather
#: than burning the retry budget (spec §15).
PERMANENT_ERRORS = (
    PermissionDenied,
    PolicyConflict,
    ValidationError,
    ApprovalRequired,
    GateBlocked,
    LookupError,
)


class Worker:
    def __init__(self, worker_id: str | None = None) -> None:
        settings = get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.poll_interval = settings.worker_poll_interval_s
        self.batch_size = max(1, settings.worker_concurrency)
        self._running = False

    def stop(self, *_args) -> None:
        log.info("worker.stop_requested", worker_id=self.worker_id)
        self._running = False

    def run_forever(self) -> None:
        settings = get_settings()
        configure(settings.log_level, json_output=settings.is_production)
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        self._running = True
        log.info("worker.started", worker_id=self.worker_id, batch_size=self.batch_size)

        idle_cycles = 0
        while self._running:
            processed = self.tick()
            if processed:
                idle_cycles = 0
            else:
                idle_cycles += 1
                # Back off gently when there is nothing to do, but keep the
                # maintenance cadence predictable.
                time.sleep(min(self.poll_interval * min(idle_cycles, 5), 10))
            if idle_cycles and idle_cycles % 30 == 0:
                self.maintenance()

        log.info("worker.stopped", worker_id=self.worker_id)

    def tick(self) -> int:
        """Claim and process one batch. Returns how many jobs ran."""
        with session_scope() as session:
            claimed = jobs.claim(session, worker_id=self.worker_id, limit=self.batch_size)
            job_ids = [j.id for j in claimed]

        for job_id in job_ids:
            self._process(job_id)

        with session_scope() as session:
            events.dispatch_pending(session)

        return len(job_ids)

    def _process(self, job_id: str) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            set_correlation_id(job.correlation_id or job.id)
            bind_context(job_id=job.id, job_kind=job.kind, worker_id=self.worker_id)
            started = time.perf_counter()
            try:
                result = jobs.execute(session, job)
                jobs.complete(session, job)
                log.info(
                    "job.succeeded",
                    job_id=job.id,
                    kind=job.kind,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    result=result,
                )
            except BudgetExceeded as exc:
                # Not a defect: the pipeline is paused until a human decides.
                jobs.fail(session, job, f"budget: {exc.message}", permanent=True)
                log.warning("job.budget_blocked", job_id=job.id, kind=job.kind)
            except PERMANENT_ERRORS as exc:
                jobs.fail(session, job, f"{type(exc).__name__}: {exc}", permanent=True)
                log.error("job.permanent_failure", job_id=job.id, kind=job.kind, error=str(exc)[:400])
            except ProviderError as exc:
                jobs.fail(session, job, str(exc), permanent=exc.permanent)
                log.warning("job.provider_failure", job_id=job.id, kind=job.kind)
            except Exception as exc:
                jobs.fail(session, job, f"{type(exc).__name__}: {exc}")
                log.exception("job.failed", job_id=job.id, kind=job.kind)
            finally:
                clear_context()

    def maintenance(self) -> None:
        with session_scope() as session:
            from app.core import idempotency
            from app.runners import manager as runner_manager

            orphans = runner_manager.reconcile_orphans(session)
            purged = idempotency.purge_expired(session)
            if orphans or purged:
                log.info("worker.maintenance", orphans=orphans, idempotency_purged=purged)


def main() -> None:  # pragma: no cover - process entry point
    Worker().run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
