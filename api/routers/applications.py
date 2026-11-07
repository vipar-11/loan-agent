"""
/api/v1/applications — loan application resource.

Submission, queue listing, detail, audit trail, review dossier, ratio recalculation,
pipeline dispatch, and human-in-the-loop determination.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from api.dependencies import get_checkpointer, get_db
from api.schemas import (
    ApplicationCreate,
    ApplicationCreateResponse,
    ApplicationDetail,
    ApplicationSummary,
    AuditEvent,
    Dossier,
    HumanDecisionRequest,
    Message,
    RatioRecalculateRequest,
    RatiosResponse,
    WorkflowItem,
    WorkflowScheduledResponse,
)
from api.services import applications as app_service
from api.services import dossier as dossier_service
from api.services import workflows as workflow_service
from loan_agent.db import ApplicationDatabase

router = APIRouter(prefix="/api/v1/applications", tags=["Applications"])


def _get_or_404(db: ApplicationDatabase, application_id: str) -> Dict[str, Any]:
    record = db.get_application(application_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Application '{application_id}' not found.")
    return record


@router.post(
    "",
    response_model=ApplicationCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a loan application",
)
def submit_application(
    payload: ApplicationCreate,
    db: ApplicationDatabase = Depends(get_db),
) -> ApplicationCreateResponse:
    """
    Records the application with status **NEW** and writes the APPLICATION_SUBMITTED audit event.

    The applicant never triggers underwriting. The agentic underwriting workflow is launched on demand or on a
    schedule by the institution (`python -m initiate_workflow`, or `POST /api/v1/workflows/run`).
    """
    record = app_service.create_application(db, payload)
    return ApplicationCreateResponse(
        application=app_service.to_detail(record),
        message=(
            f"Thank you, {record['applicant_name']} — your loan application has been received. "
            f"Your reference number is {record['application_id']}. Our underwriting team will review your "
            f"application and notify you of the decision. No further action is needed from you at this time."
        ),
    )


@router.get("", response_model=List[ApplicationSummary], summary="List applications")
def list_applications(
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="new | processing | pending_review | adjudicated | approved | rejected | all (default)",
    ),
    db: ApplicationDatabase = Depends(get_db),
) -> List[ApplicationSummary]:
    return [app_service.to_summary(a) for a in app_service.list_applications(db, status_filter)]


@router.get("/{application_id}", response_model=ApplicationDetail, responses={404: {"model": Message}})
def get_application(application_id: str, db: ApplicationDatabase = Depends(get_db)) -> ApplicationDetail:
    """Full record including the persisted workflow state and adjudication."""
    return app_service.to_detail(_get_or_404(db, application_id))


@router.get("/{application_id}/audit-trail", response_model=List[AuditEvent], responses={404: {"model": Message}})
def get_audit_trail(application_id: str, db: ApplicationDatabase = Depends(get_db)) -> List[AuditEvent]:
    record = _get_or_404(db, application_id)
    return [AuditEvent(**e) for e in db.get_audit_trail(record["application_id"])]


@router.get("/{application_id}/dossier", response_model=Dossier, responses={404: {"model": Message}})
def get_dossier(application_id: str, db: ApplicationDatabase = Depends(get_db)) -> Dossier:
    """Underwriter review dossier: AI assessment, tradelines, AI-classified cash flow, ratios, sizing."""
    return dossier_service.build_dossier(_get_or_404(db, application_id))


@router.get(
    "/{application_id}/classifications/heuristic",
    response_model=Dict[str, str],
    responses={404: {"model": Message}},
    summary="Heuristic (rule-based) transaction classifications",
)
def heuristic_classifications(application_id: str, db: ApplicationDatabase = Depends(get_db)) -> Dict[str, str]:
    """Deterministic fallback classification for every ledger entry, keyed by tx_id."""
    return dossier_service.heuristic_classifications(_get_or_404(db, application_id))


@router.post(
    "/{application_id}/ratios/recalculate",
    response_model=RatiosResponse,
    responses={404: {"model": Message}},
    summary="Recalculate financial ratios with officer classifications",
)
def recalculate_ratios(
    application_id: str,
    payload: RatioRecalculateRequest,
    db: ApplicationDatabase = Depends(get_db),
) -> RatiosResponse:
    """Deterministic SR 11-7 recalculation. Does not mutate the persisted graph state."""
    return dossier_service.recalculate_ratios(_get_or_404(db, application_id), payload.classifications)


@router.post(
    "/{application_id}/workflow",
    response_model=WorkflowScheduledResponse | WorkflowItem,
    status_code=status.HTTP_202_ACCEPTED,
    responses={404: {"model": Message}, 409: {"model": Message}},
    summary="Run the agentic pipeline for one application",
)
def run_workflow(
    application_id: str,
    background: BackgroundTasks,
    sync: bool = Query(False, description="Wait for completion and return the outcome (default: background)."),
    force: bool = Query(False, description="Allow retrying an application stuck in PENDING_PROCESSING."),
    db: ApplicationDatabase = Depends(get_db),
    checkpointer: Any = Depends(get_checkpointer),
):
    record = _get_or_404(db, application_id)
    try:
        workflow_service.assert_dispatchable(record, force=force)
    except workflow_service.WorkflowStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if sync:
        return WorkflowItem(**workflow_service.run_for_application(record, db, checkpointer, initiator="API"))

    record = workflow_service.mark_scheduled(db, record, actor="API")
    background.add_task(workflow_service.run_for_application, record, db, checkpointer, "API")
    return WorkflowScheduledResponse(
        application_id=record["application_id"],
        status=record["status"],
        scheduled=True,
        message="Agentic pipeline scheduled. Poll GET /api/v1/applications/{id} for the outcome.",
    )


@router.post(
    "/{application_id}/decision",
    response_model=ApplicationDetail,
    responses={404: {"model": Message}, 409: {"model": Message}},
    summary="Record the human credit officer determination",
)
def record_decision(
    application_id: str,
    payload: HumanDecisionRequest,
    db: ApplicationDatabase = Depends(get_db),
    checkpointer: Any = Depends(get_checkpointer),
) -> ApplicationDetail:
    """
    Human-in-the-loop resumption: injects the determination into the paused workflow and resumes it,
    finalizing the file as **HUMAN_APPROVED** or **HUMAN_DENIED**.
    """
    _get_or_404(db, application_id)
    try:
        record = workflow_service.record_human_decision(application_id, payload, db, checkpointer)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return app_service.to_detail(record)
