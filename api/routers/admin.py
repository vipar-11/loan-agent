"""
/api/v1/admin — administrator monitoring & configuration.

Status monitoring by the five main statuses, initiate_workflow run health/history,
the persisted schedule configuration, and the partner batch-intake monitor. Nothing here deletes data.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from api.dependencies import get_db
from api.schemas import (
    IntakeBatch,
    IntakeBatchDetail,
    IntakeRow,
    IntakeScanResult,
    IntakeStatus,
    Message,
    ScheduleConfig,
    ScheduleConfigUpdate,
    SchedulerStatus,
    StatusGroup,
    WorkflowRun,
)
from api.services import batch_intake as intake_service
from api.services import scheduler as scheduler_service
from loan_agent.db import ApplicationDatabase

router = APIRouter(prefix="/api/v1/admin", tags=["Administration"])


@router.get("/status-summary", response_model=List[StatusGroup], summary="Application counts by main status")
def status_summary(db: ApplicationDatabase = Depends(get_db)) -> List[StatusGroup]:
    """new · pending processing · pending review · approved · rejected — each with its sub-status breakdown."""
    return [StatusGroup(**g) for g in db.status_summary()]


@router.get("/scheduler", response_model=SchedulerStatus, summary="Is initiate_workflow running periodically?")
def scheduler_status(db: ApplicationDatabase = Depends(get_db)) -> SchedulerStatus:
    return scheduler_service.scheduler_status(db)


@router.get("/scheduler/config", response_model=ScheduleConfig, summary="Current schedule configuration")
def get_schedule_config(db: ApplicationDatabase = Depends(get_db)) -> ScheduleConfig:
    return scheduler_service.get_schedule_config(db)


@router.put("/scheduler/config", response_model=ScheduleConfig, summary="Update the run frequency / enablement")
def update_schedule_config(payload: ScheduleConfigUpdate, db: ApplicationDatabase = Depends(get_db)) -> ScheduleConfig:
    """Sets the expected interval (minutes) between two initiate_workflow runs and/or toggles the built-in scheduler."""
    return scheduler_service.update_schedule_config(db, payload, actor=payload.updated_by)


@router.get("/workflow-runs", response_model=List[WorkflowRun], summary="Latest initiate_workflow runs")
def workflow_runs(
    limit: int = Query(10, ge=1, le=100, description="Number of most recent runs to return (default 10)."),
    db: ApplicationDatabase = Depends(get_db),
) -> List[WorkflowRun]:
    return scheduler_service.list_runs(db, limit=limit)


# ---------------------------------------------------------------------------
# Partner batch intake (bulk applications delivered over FTP)
# ---------------------------------------------------------------------------
def _intake_monitoring(request: Request) -> bool:
    task = getattr(request.app.state, "intake_task", None)
    return task is not None and not task.done()


@router.get("/intake", response_model=IntakeStatus, summary="Partner batch-intake monitor status")
def intake_status(request: Request, db: ApplicationDatabase = Depends(get_db)) -> IntakeStatus:
    cfg = request.app.state.intake_config
    return IntakeStatus(**intake_service.status_snapshot(db, cfg, monitoring=_intake_monitoring(request)))


@router.get("/intake/batches", response_model=List[IntakeBatch], summary="Recently received partner batch files")
def intake_batches(
    limit: int = Query(25, ge=1, le=200),
    partner_id: Optional[str] = Query(None, max_length=64),
    db: ApplicationDatabase = Depends(get_db),
) -> List[IntakeBatch]:
    return [IntakeBatch(**b) for b in db.list_intake_batches(limit=limit, partner_id=partner_id)]


@router.get(
    "/intake/batches/{batch_id}",
    response_model=IntakeBatchDetail,
    responses={404: {"model": Message}},
    summary="One batch file with the outcome of every row (accepted / flagged with reasons)",
)
def intake_batch_detail(batch_id: int, db: ApplicationDatabase = Depends(get_db)) -> IntakeBatchDetail:
    batch = db.get_intake_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Intake batch #{batch_id} not found.")
    rows = [IntakeRow(**r) for r in db.list_intake_rows(batch_id)]
    return IntakeBatchDetail(**batch, rows=rows)


@router.post("/intake/scan", response_model=IntakeScanResult, summary="Scan the partner inboxes now")
def intake_scan(request: Request, db: ApplicationDatabase = Depends(get_db)) -> IntakeScanResult:
    """Processes every settled file waiting in the approved partners' inboxes immediately (same rules as the monitor)."""
    scanned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    batches = intake_service.scan_inbox(db, request.app.state.intake_config)
    return IntakeScanResult(scanned_at=scanned_at, batches=[IntakeBatch(**b) for b in batches])
