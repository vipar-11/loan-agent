"""
initiate_workflow scheduling, run logging, and health monitoring.

Every execution of the "initiate workflow" job — whether triggered by the built-in periodic scheduler,
the `initiate_workflow` CLI/cron script, or a direct API call — is recorded in the `workflow_runs` table.
The administrator screen uses that log to verify the job is running at the configured cadence.

Configuration (persisted in `system_settings`, survives restarts):
    initiate_workflow.interval_minutes   expected gap between two runs (default 2)
    initiate_workflow.scheduler_enabled  whether the service itself fires the job on that cadence (default true)
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from api.schemas import ScheduleConfig, ScheduleConfigUpdate, SchedulerStatus, WorkflowRun
from loan_agent.db import ApplicationDatabase

logger = logging.getLogger("apex.scheduler")

DEFAULT_INTERVAL_MINUTES = 2
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 24 * 60
OVERDUE_GRACE_FACTOR = 1.5  # a run is overdue once 1.5x the expected interval has elapsed
TICK_SECONDS = 5

SETTING_INTERVAL = "initiate_workflow.interval_minutes"
SETTING_ENABLED = "initiate_workflow.scheduler_enabled"

TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"
ACTIVE_RUN_STATUSES = {"REQUESTED", "RUNNING"}


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _fmt(dt: datetime) -> str:
    return dt.strftime(TIMESTAMP_FMT)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, TIMESTAMP_FMT)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def get_schedule_config(db: ApplicationDatabase) -> ScheduleConfig:
    raw_interval = db.get_setting(SETTING_INTERVAL, str(DEFAULT_INTERVAL_MINUTES))
    raw_enabled = db.get_setting(SETTING_ENABLED, "true")
    try:
        interval = int(float(raw_interval))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_MINUTES
    interval = min(max(interval, MIN_INTERVAL_MINUTES), MAX_INTERVAL_MINUTES)
    record = db.get_setting_record(SETTING_INTERVAL) or db.get_setting_record(SETTING_ENABLED)
    return ScheduleConfig(
        interval_minutes=interval,
        scheduler_enabled=str(raw_enabled).strip().lower() in ("1", "true", "yes", "on"),
        updated_at=record.get("updated_at") if record else None,
        updated_by=record.get("updated_by") if record else None,
    )


def update_schedule_config(db: ApplicationDatabase, payload: ScheduleConfigUpdate, actor: str) -> ScheduleConfig:
    if payload.interval_minutes is not None:
        db.set_setting(SETTING_INTERVAL, int(payload.interval_minutes), updated_by=actor)
    if payload.scheduler_enabled is not None:
        db.set_setting(SETTING_ENABLED, "true" if payload.scheduler_enabled else "false", updated_by=actor)
    return get_schedule_config(db)


# ---------------------------------------------------------------------------
# Run logging & execution
# ---------------------------------------------------------------------------
def request_run(db: ApplicationDatabase, trigger_source: str) -> Dict[str, Any]:
    """Logs a REQUESTED run (fire-and-forget callers return immediately after this)."""
    run = db.create_workflow_run(trigger_source=trigger_source, status="REQUESTED")
    run = db.update_workflow_run(run["id"], {"found": db.count_applications(status="new")})
    return run  # type: ignore[return-value]


def execute_run(
    db: ApplicationDatabase,
    checkpointer: Any,
    run_id: int,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Executes a previously requested run and records its outcome. Never raises."""
    from api.services.workflows import run_batch  # local import: avoids an import cycle

    db.update_workflow_run(run_id, {"status": "RUNNING", "started_at": _fmt(_now())})
    try:
        summary = run_batch(db, checkpointer, limit=limit, dry_run=False)
        updates = {
            "status": "COMPLETED",
            "finished_at": _fmt(_now()),
            "found": summary["found"],
            "selected": summary["selected"],
            "approved": summary["approved"],
            "rejected": summary["rejected"],
            "escalated": summary["escalated"],
            "errors": summary["errors"],
        }
    except Exception as exc:  # pragma: no cover - defensive; run_batch itself never raises per application
        logger.exception("initiate_workflow run %s failed", run_id)
        updates = {"status": "FAILED", "finished_at": _fmt(_now()), "error_message": str(exc)}
    return db.update_workflow_run(run_id, updates)  # type: ignore[return-value]


def list_runs(db: ApplicationDatabase, limit: int = 50) -> List[WorkflowRun]:
    return [WorkflowRun(**r) for r in db.list_workflow_runs(limit=limit)]


# ---------------------------------------------------------------------------
# Health evaluation (is the job running periodically?)
# ---------------------------------------------------------------------------
def scheduler_status(db: ApplicationDatabase, now: Optional[datetime] = None) -> SchedulerStatus:
    now = now or _now()
    config = get_schedule_config(db)
    last = db.get_last_workflow_run()
    interval = timedelta(minutes=config.interval_minutes)

    if last is None:
        return SchedulerStatus(
            config=config,
            health="NEVER_RUN",
            message="The initiate_workflow job has never run. New applications will not be underwritten until it does.",
            next_due_at=_fmt(now) if config.scheduler_enabled else None,
        )

    last_at = _parse(last.get("requested_at")) or now
    elapsed = now - last_at
    next_due = last_at + interval
    base = dict(
        config=config,
        last_run=WorkflowRun(**last),
        seconds_since_last_run=int(elapsed.total_seconds()),
        next_due_at=_fmt(next_due),
    )

    if last.get("status") in ACTIVE_RUN_STATUSES:
        return SchedulerStatus(**base, health="RUNNING", message="An initiate_workflow run is currently in progress.")
    if elapsed <= interval * OVERDUE_GRACE_FACTOR:
        return SchedulerStatus(
            **base,
            health="HEALTHY",
            message=f"Last run {int(elapsed.total_seconds() // 60)} min ago; within the expected "
                    f"{config.interval_minutes}-minute cadence.",
        )
    return SchedulerStatus(
        **base,
        health="OVERDUE",
        message=f"No run for {int(elapsed.total_seconds() // 60)} min — exceeds the expected "
                f"{config.interval_minutes}-minute cadence. Check the scheduler / cron job.",
    )


# ---------------------------------------------------------------------------
# Built-in periodic trigger
# ---------------------------------------------------------------------------
def run_if_due(db: ApplicationDatabase, checkpointer: Any, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """
    One scheduler tick: fires a run when the scheduler is enabled, no run is active, and the
    configured interval has elapsed since the last run (from any trigger). Returns the run record if fired.
    """
    now = now or _now()
    config = get_schedule_config(db)
    if not config.scheduler_enabled:
        return None
    last = db.get_last_workflow_run()
    if last:
        if last.get("status") in ACTIVE_RUN_STATUSES:
            return None
        last_at = _parse(last.get("requested_at")) or now
        if now - last_at < timedelta(minutes=config.interval_minutes):
            return None
    run = request_run(db, trigger_source="SCHEDULER")
    logger.info("Scheduler firing initiate_workflow run #%s", run["id"])
    return execute_run(db, checkpointer, run["id"])


async def scheduler_loop(db: ApplicationDatabase, checkpointer: Any, stop: asyncio.Event, tick_seconds: float = TICK_SECONDS) -> None:
    """Background task started by the application lifespan; exits promptly when `stop` is set."""
    logger.info("initiate_workflow scheduler started (tick=%ss)", tick_seconds)
    while not stop.is_set():
        try:
            await asyncio.to_thread(run_if_due, db, checkpointer)
        except Exception:  # pragma: no cover - keep the loop alive no matter what
            logger.exception("Scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("initiate_workflow scheduler stopped")
