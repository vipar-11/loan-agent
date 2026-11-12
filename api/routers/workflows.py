"""
/api/v1/workflows — the "initiate workflow" job (API equivalent of the initiate_workflow scheduler) and queue stats.

Running the workflow requires the Administrator role (workflow:run); queue stats require application:read.
"""
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from api.dependencies import get_checkpointer, get_db, require_permission
from api.schemas import Message, QueueStats, WorkflowRun, WorkflowRunAccepted, WorkflowRunRequest, WorkflowRunSummary
from api.services import applications as app_service
from api.services import scheduler as scheduler_service
from api.services import workflows as workflow_service
from loan_agent.auth import Permission
from loan_agent.db import ApplicationDatabase

RUN_WORKFLOWS = [Depends(require_permission(Permission.WORKFLOW_RUN))]

router = APIRouter(prefix="/api/v1", tags=["Workflows"])


@router.post(
    "/workflows/run",
    response_model=WorkflowRunAccepted | WorkflowRunSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Initiate the approval workflow for all NEW applications",
    dependencies=RUN_WORKFLOWS,
)
def run_pending_workflows(
    payload: WorkflowRunRequest,
    background: BackgroundTasks,
    sync: bool = Query(False, description="Wait for completion and return the full summary (default: fire-and-forget)."),
    trigger: str = Query("API", max_length=40, description="Who requested the run (for the run log), e.g. CLI, API."),
    db: ApplicationDatabase = Depends(get_db),
    checkpointer: Any = Depends(get_checkpointer),
):
    """
    Default is **fire-and-forget**: the run is logged as REQUESTED, executed in the background, and the caller
    receives the run id immediately. Dry runs are always synchronous and are not logged.
    """
    if payload.dry_run:
        return WorkflowRunSummary(**workflow_service.run_batch(db, checkpointer, payload.limit, dry_run=True))

    run = scheduler_service.request_run(db, trigger_source=trigger.upper())
    if sync:
        scheduler_service.execute_run(db, checkpointer, run["id"], limit=payload.limit)
        return WorkflowRunSummary(**workflow_service.run_batch_summary_for(db, run["id"]))

    background.add_task(scheduler_service.execute_run, db, checkpointer, run["id"], payload.limit)
    return WorkflowRunAccepted(
        run_id=run["id"],
        status=run["status"],
        found=run["found"],
        message=(
            f"Run #{run['id']} accepted: {run['found']} new application(s) queued for underwriting. "
            f"Track progress via GET /api/v1/workflows/runs/{run['id']} or the Administrator screen."
        ),
    )


@router.get(
    "/workflows/runs/{run_id}", response_model=WorkflowRun, responses={404: {"model": Message}},
    dependencies=RUN_WORKFLOWS,
)
def get_workflow_run(run_id: int, db: ApplicationDatabase = Depends(get_db)) -> WorkflowRun:
    run = db.get_workflow_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow run #{run_id} not found.")
    return WorkflowRun(**run)


@router.get(
    "/stats", response_model=QueueStats, summary="Queue statistics",
    dependencies=[Depends(require_permission(Permission.APPLICATION_READ))],
)
def get_stats(db: ApplicationDatabase = Depends(get_db)) -> QueueStats:
    return app_service.queue_stats(db)
