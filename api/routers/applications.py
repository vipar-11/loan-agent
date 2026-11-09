"""
/api/v1/applications — loan application resource.

Submission, queue listing, detail, audit trail, review dossier, ratio recalculation,
pipeline dispatch, review checkout (one identity per file), and human-in-the-loop determination.

The caller's identity comes from `establish_user_identity()` (today: one anonymous session per browser tab, carried
in the X-Apex-Session-Id header).
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from api.dependencies import establish_user_identity, get_checkpointer, get_db
from api.schemas import (
    ApplicationCreate,
    ApplicationCreateResponse,
    ApplicationDetail,
    ApplicationSummary,
    AuditEvent,
    CheckoutInfo,
    CheckoutReleaseResponse,
    Dossier,
    HumanDecisionRequest,
    Message,
    RatioRecalculateRequest,
    RatiosResponse,
    UserIdentity,
    WorkflowItem,
    WorkflowScheduledResponse,
)
from api.services import applications as app_service
from api.services import checkout as checkout_service
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
    identity: UserIdentity = Depends(establish_user_identity),
) -> List[ApplicationSummary]:
    checkouts = checkout_service.active_checkouts(db, viewer=identity)
    return [
        app_service.to_summary(a, checkouts.get(a["application_id"]))
        for a in app_service.list_applications(db, status_filter)
    ]


@router.get("/{application_id}", response_model=ApplicationDetail, responses={404: {"model": Message}})
def get_application(
    application_id: str,
    db: ApplicationDatabase = Depends(get_db),
    identity: UserIdentity = Depends(establish_user_identity),
) -> ApplicationDetail:
    """Full record including the persisted workflow state, adjudication and active review checkout."""
    record = _get_or_404(db, application_id)
    return app_service.to_detail(record, checkout_service.active_checkout(db, record["application_id"], viewer=identity))


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
    "/{application_id}/checkout",
    response_model=CheckoutInfo,
    responses={404: {"model": Message}, 409: {"model": Message}},
    summary="Check out a PENDING_REVIEW file for review by the calling identity",
)
def checkout_application(
    application_id: str,
    db: ApplicationDatabase = Depends(get_db),
    identity: UserIdentity = Depends(establish_user_identity),
) -> CheckoutInfo:
    """
    Opens the file for the calling identity only (today: the browser tab's session). Every other identity — another
    reviewer or another tab — gets **409** until the holder closes it or the lease (`ttl_seconds`) lapses. While the
    review stays open, the holder renews the lease every `heartbeat_seconds` via `POST /{id}/checkout/renew`.
    Re-opening a file you already hold is allowed.
    """
    try:
        return checkout_service.checkout(db, application_id, identity)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'\"")) from exc
    except (ValueError, checkout_service.CheckoutConflict) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/{application_id}/checkout/renew",
    response_model=CheckoutInfo,
    responses={404: {"model": Message}, 409: {"model": Message}},
    summary="Heartbeat: extend the lease of a checkout you hold",
)
def renew_checkout(
    application_id: str,
    db: ApplicationDatabase = Depends(get_db),
    identity: UserIdentity = Depends(establish_user_identity),
) -> CheckoutInfo:
    """**409** when the caller no longer holds the file (force-released, or lapsed and taken by someone else)."""
    try:
        return checkout_service.renew(db, application_id, identity)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'\"")) from exc
    except checkout_service.CheckoutConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete(
    "/{application_id}/checkout",
    response_model=CheckoutReleaseResponse,
    responses={404: {"model": Message}},
    summary="Release a review checkout",
)
def release_checkout(
    application_id: str,
    force: bool = Query(False, description="Administrator override: release regardless of the holder."),
    actor: Optional[str] = Query(None, max_length=120, description="Who forced the release (audit trail)."),
    db: ApplicationDatabase = Depends(get_db),
    identity: UserIdentity = Depends(establish_user_identity),
) -> CheckoutReleaseResponse:
    """
    Releases the caller's own checkout. Idempotent: releasing a checkout you do not hold (or that no longer exists)
    is a no-op. `force=true` is an administrator override (audited).
    """
    try:
        released = checkout_service.release(db, application_id, identity, force=force, actor=actor)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'\"")) from exc
    return CheckoutReleaseResponse(
        application_id=application_id,
        released=released,
        message="Checkout released." if released else "No checkout held by this session; nothing to release.",
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
    identity: UserIdentity = Depends(establish_user_identity),
) -> ApplicationDetail:
    """
    Human-in-the-loop resumption: injects the determination into the paused workflow and resumes it,
    finalizing the file as **HUMAN_APPROVED** or **HUMAN_DENIED**.

    The calling identity must hold the file's checkout (one is taken implicitly when nobody holds it);
    **409** if another identity has the file checked out. The checkout is released once the decision is recorded.
    """
    _get_or_404(db, application_id)
    try:
        record = workflow_service.record_human_decision(application_id, payload, db, checkpointer, identity)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (ValueError, checkout_service.CheckoutConflict) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return app_service.to_detail(record)
