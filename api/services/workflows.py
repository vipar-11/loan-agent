"""
Workflow orchestration services: single-application runs, batch runs, and human-in-the-loop decisions.

Thin wrappers over `loan_agent.workflow_runner` that add API-specific concerns:
state-machine guards (which statuses may be dispatched), scheduling semantics, and execution serialization.
"""
import logging
from typing import Any, Dict, Optional

from api.dependencies import WORKFLOW_LOCK
from api.schemas import HumanDecisionRequest
from loan_agent.db import ApplicationDatabase
from loan_agent.workflow_runner import (
    process_application,
    resume_with_human_decision,
    run_initiate_workflow,
)

logger = logging.getLogger("apex.workflows")

DISPATCHABLE_STATUSES = {"NEW"}
RETRYABLE_STATUSES = {"PENDING_PROCESSING"}


class WorkflowStateError(Exception):
    """Raised when an application is not in a status that allows the requested transition."""


def assert_dispatchable(record: Dict[str, Any], force: bool = False) -> None:
    status = (record.get("status") or "").upper()
    if status in DISPATCHABLE_STATUSES:
        return
    if force and status in RETRYABLE_STATUSES:
        return
    raise WorkflowStateError(
        f"Application '{record['application_id']}' cannot be dispatched from status {status}. "
        f"Only NEW applications (or PENDING_PROCESSING with force=true) may start the pipeline."
    )


def mark_scheduled(db: ApplicationDatabase, record: Dict[str, Any], actor: str = "API") -> Dict[str, Any]:
    """Synchronously flags the record as in-flight before handing off to a background worker."""
    db.update_application(
        application_id=record["application_id"],
        updates={"status": "PENDING_PROCESSING"},
        actor=actor,
        event_type="WORKFLOW_SCHEDULED",
        details="Agentic pipeline execution scheduled via API.",
    )
    return db.get_application(record["application_id"])  # type: ignore[return-value]


def run_for_application(
    record: Dict[str, Any],
    db: ApplicationDatabase,
    checkpointer: Any,
    initiator: str = "API",
) -> Dict[str, Any]:
    """Executes the pipeline for one application (serialized). Never raises on pipeline failure."""
    with WORKFLOW_LOCK:
        logger.info("Running underwriting pipeline for %s", record["application_id"])
        item = process_application(record, app_db=db, checkpointer=checkpointer, initiator=initiator)
        logger.info("Pipeline finished for %s -> %s", record["application_id"], item.get("status"))
        return item


def run_batch(
    db: ApplicationDatabase,
    checkpointer: Any,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Batch-runs the pipeline for every NEW application (API equivalent of `python -m initiate_workflow`)."""
    with WORKFLOW_LOCK:
        return run_initiate_workflow(dry_run=dry_run, limit=limit, app_db=db, checkpointer=checkpointer)


def run_batch_summary_for(db: ApplicationDatabase, run_id: int) -> Dict[str, Any]:
    """Projects a completed run-log record onto the WorkflowRunSummary shape (items are not persisted)."""
    run = db.get_workflow_run(run_id) or {}
    return {
        "found": run.get("found", 0),
        "selected": run.get("selected", 0),
        "dry_run": False,
        "approved": run.get("approved", 0),
        "rejected": run.get("rejected", 0),
        "escalated": run.get("escalated", 0),
        "errors": run.get("errors", 0),
        "items": [],
    }


def record_human_decision(
    application_id: str,
    payload: HumanDecisionRequest,
    db: ApplicationDatabase,
    checkpointer: Any,
) -> Dict[str, Any]:
    """
    Resumes the paused graph with the officer's determination.
    Raises KeyError (unknown application) or ValueError (wrong state / missing notes).
    """
    with WORKFLOW_LOCK:
        return resume_with_human_decision(
            application_id=application_id,
            decision=payload.decision,
            notes=payload.notes,
            officer=payload.officer,
            app_db=db,
            checkpointer=checkpointer,
        )
