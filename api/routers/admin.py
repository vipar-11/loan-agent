"""
/api/v1/admin — administrator monitoring & configuration.

Status monitoring by the five main statuses, initiate_workflow run health/history,
and the persisted schedule configuration. Nothing here deletes data.
"""
from typing import List

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_db
from api.schemas import ScheduleConfig, ScheduleConfigUpdate, SchedulerStatus, StatusGroup, WorkflowRun
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
