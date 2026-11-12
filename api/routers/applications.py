"""
/api/v1/applications — loan application resource.

Submission, queue listing, detail, audit trail, review dossier, ratio recalculation,
pipeline dispatch, review checkout (one identity per file), and human-in-the-loop determination.

Every endpoint requires a signed-in user holding the permission named in its signature (role-based access control,
see loan_agent/auth/roles.py). Review checkouts are held by the signed-in user in one browser tab.
"""
from datetime import date
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from api.dependencies import ACCESS_DENIED, get_checkpointer, get_db, require_any_permission, require_permission
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
    LoanPurpose,
    Message,
    MyApplicationStatus,
    NextReviewResponse,
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
from loan_agent import sla as sla_engine
from loan_agent.auth import Permission
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
    identity: UserIdentity = Depends(require_permission(Permission.APPLICATION_SUBMIT)),
) -> ApplicationCreateResponse:
    """
    Records the application with status **NEW** and writes the APPLICATION_SUBMITTED audit event (actor: the
    signed-in user).

    The applicant never triggers underwriting. The agentic underwriting workflow is launched on demand or on a
    schedule by the institution (`python -m initiate_workflow`, or `POST /api/v1/workflows/run`). Owned by the
    signed-in applicant (`submitted_by`): track it any time with `GET /api/v1/applications/mine`.
    """
    record = app_service.create_application(
        db, payload, actor=identity.audit_actor, submitted_by=identity.user_id
    )
    return ApplicationCreateResponse(
        application=app_service.to_detail(record),
        message=(
            f"Thank you, {record['applicant_name']} — your loan application has been received. "
            f"Your reference number is {record['application_id']}. Our underwriting team will review your "
            f"application; decisions are typically made within {sla_engine.DECISION_SLA_BUSINESS_DAYS} business "
            f"days. Track the status anytime on the My Applications page. No further action is needed from you "
            f"at this time."
        ),
    )


@router.post(
    "/next-for-review",
    response_model=NextReviewResponse,
    summary="Get Work: check out the next highest-priority file for review",
)
def next_for_review(
    db: ApplicationDatabase = Depends(get_db),
    identity: UserIdentity = Depends(require_permission(Permission.APPLICATION_UNDERWRITE)),
) -> NextReviewResponse:
    """
    Hands the caller the next file to work on, already checked out to them (user + browser tab), exactly as if they
    had opened it from the queue: a file they already hold first, otherwise the oldest PENDING_REVIEW application
    that nobody else has open. Concurrent callers never receive the same file. Queue filters do not apply — they are
    for cherry-picking; Get Work always serves the queue in priority order. `application` is null when no file is
    available.
    """
    info = checkout_service.checkout_next(db, identity)
    if info is None:
        return NextReviewResponse(
            message="No files are waiting for review right now (or every pending file is open with another reviewer)."
        )
    record = db.get_application(info.application_id)
    return NextReviewResponse(
        application=app_service.to_summary(record, info),  # type: ignore[arg-type]
        checkout=info,
        message=f"Application {info.application_id} is checked out to you for review.",
    )


@router.get(
    "/mine",
    response_model=List[MyApplicationStatus],
    summary="My Applications: track the status of your own submissions",
)
def list_my_applications(
    status_filter: Optional[str] = Query(
        None, alias="status", description="new | processing | pending_review | adjudicated | approved | rejected",
    ),
    db: ApplicationDatabase = Depends(get_db),
    identity: UserIdentity = Depends(require_permission(Permission.APPLICATION_READ_OWN)),
) -> List[MyApplicationStatus]:
    """Every application submitted under the signed-in applicant's account — never another applicant's."""
    records = app_service.list_own_applications(db, identity.user_id, status_filter)
    return [app_service.to_my_status(r) for r in records]


@router.get(
    "/mine/{application_id}",
    response_model=MyApplicationStatus,
    responses={404: {"model": Message}},
    summary="My Applications: status of one of your own submissions",
)
def get_my_application(
    application_id: str,
    db: ApplicationDatabase = Depends(get_db),
    identity: UserIdentity = Depends(require_permission(Permission.APPLICATION_READ_OWN)),
) -> MyApplicationStatus:
    """404 both for an unknown reference number and for an application that is not the caller's own (see the
    Applicant Portal's submission confirmation for the reference number)."""
    record = db.get_application(application_id)
    if not record or record.get("submitted_by") != identity.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Application '{application_id}' not found.")
    return app_service.to_my_status(record)


@router.get(
    "",
    response_model=List[ApplicationSummary],
    responses={422: {"model": Message}},
    summary="List applications (filter by loan metadata; oldest- or newest-first)",
)
def list_applications(
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="new | processing | pending_review | adjudicated | approved | rejected | all (default)",
    ),
    q: Optional[str] = Query(None, max_length=120,
                             description="Applicant name or reference number (case-insensitive, partial match)."),
    loan_purpose: Optional[LoanPurpose] = Query(None, description="Exact loan purpose."),
    min_amount: Optional[float] = Query(None, ge=0, description="Minimum loan amount requested (inclusive)."),
    max_amount: Optional[float] = Query(None, ge=0, description="Maximum loan amount requested (inclusive)."),
    submitted_from: Optional[date] = Query(None, description="Submitted on or after this day (YYYY-MM-DD)."),
    submitted_to: Optional[date] = Query(None, description="Submitted on or before this day (YYYY-MM-DD)."),
    decided_by_me: bool = Query(
        False, description="Only applications whose determination was recorded by the signed-in user."
    ),
    sort: Literal["newest", "oldest"] = Query(
        "newest", description="By submission time. The Underwriter Desk works the pending review queue "
                              "`oldest` first, so the longest-waiting applications are decided first."
    ),
    db: ApplicationDatabase = Depends(get_db),
    identity: UserIdentity = Depends(require_permission(Permission.APPLICATION_READ)),
) -> List[ApplicationSummary]:
    filters = app_service.ApplicationFilters(
        q=q, loan_purpose=loan_purpose, min_amount=min_amount, max_amount=max_amount,
        submitted_from=submitted_from, submitted_to=submitted_to,
        decided_by=identity.user_id if decided_by_me else None,
    )
    try:
        filters.validate()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    checkouts = checkout_service.active_checkouts(db, viewer=identity)
    records = app_service.query_applications(db, status_filter, filters, sort)  # one filtered, ordered SQL query
    return [app_service.to_summary(a, checkouts.get(a["application_id"])) for a in records]


@router.get("/{application_id}", response_model=ApplicationDetail, responses={404: {"model": Message}})
def get_application(
    application_id: str,
    db: ApplicationDatabase = Depends(get_db),
    identity: UserIdentity = Depends(require_permission(Permission.APPLICATION_READ)),
) -> ApplicationDetail:
    """Full record including the persisted workflow state, adjudication and active review checkout."""
    record = _get_or_404(db, application_id)
    return app_service.to_detail(record, checkout_service.active_checkout(db, record["application_id"], viewer=identity))


@router.get(
    "/{application_id}/audit-trail",
    response_model=List[AuditEvent],
    responses={404: {"model": Message}},
    dependencies=[Depends(require_permission(Permission.APPLICATION_READ))],
)
def get_audit_trail(application_id: str, db: ApplicationDatabase = Depends(get_db)) -> List[AuditEvent]:
    record = _get_or_404(db, application_id)
    return [AuditEvent(**e) for e in db.get_audit_trail(record["application_id"])]


@router.get(
    "/{application_id}/dossier",
    response_model=Dossier,
    responses={404: {"model": Message}},
    dependencies=[Depends(require_permission(Permission.APPLICATION_UNDERWRITE))],
)
def get_dossier(application_id: str, db: ApplicationDatabase = Depends(get_db)) -> Dossier:
    """Underwriter review dossier: AI assessment, tradelines, AI-classified cash flow, ratios, sizing."""
    return dossier_service.build_dossier(_get_or_404(db, application_id))


@router.get(
    "/{application_id}/classifications/heuristic",
    response_model=Dict[str, str],
    responses={404: {"model": Message}},
    summary="Heuristic (rule-based) transaction classifications",
    dependencies=[Depends(require_permission(Permission.APPLICATION_UNDERWRITE))],
)
def heuristic_classifications(application_id: str, db: ApplicationDatabase = Depends(get_db)) -> Dict[str, str]:
    """Deterministic fallback classification for every ledger entry, keyed by tx_id."""
    return dossier_service.heuristic_classifications(_get_or_404(db, application_id))


@router.post(
    "/{application_id}/ratios/recalculate",
    response_model=RatiosResponse,
    responses={404: {"model": Message}},
    summary="Recalculate financial ratios with officer classifications",
    dependencies=[Depends(require_permission(Permission.APPLICATION_UNDERWRITE))],
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
    identity: UserIdentity = Depends(require_permission(Permission.WORKFLOW_RUN)),
):
    """Dispatches the underwriting workflow for one NEW application."""
    record = _get_or_404(db, application_id)
    try:
        workflow_service.assert_dispatchable(record, force=force)
    except workflow_service.WorkflowStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if sync:
        return WorkflowItem(**workflow_service.run_for_application(record, db, checkpointer,
                                                                   initiator=identity.audit_actor))

    record = workflow_service.mark_scheduled(db, record, actor=identity.audit_actor)
    background.add_task(workflow_service.run_for_application, record, db, checkpointer, identity.audit_actor)
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
    identity: UserIdentity = Depends(require_permission(Permission.APPLICATION_UNDERWRITE)),
) -> CheckoutInfo:
    """
    Opens the file for the signed-in underwriter in the calling browser tab only. Everybody else — another
    reviewer, or another tab — gets **409** until the holder closes it or the lease (`ttl_seconds`) lapses. While the
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
    identity: UserIdentity = Depends(require_permission(Permission.APPLICATION_UNDERWRITE)),
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
    force: bool = Query(False, description="Override: release regardless of the holder (audited)."),
    db: ApplicationDatabase = Depends(get_db),
    identity: UserIdentity = Depends(
        require_any_permission(Permission.APPLICATION_UNDERWRITE, Permission.CHECKOUT_OVERRIDE)
    ),
) -> CheckoutReleaseResponse:
    """
    Releases the caller's own checkout. Idempotent: releasing a checkout you do not hold (or that no longer exists)
    is a no-op. `force=true` releases the checkout regardless of the holder (audited against the caller).
    """
    if force and Permission.CHECKOUT_OVERRIDE.value not in identity.permissions:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ACCESS_DENIED)
    try:
        released = checkout_service.release(db, application_id, identity, force=force)
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
    identity: UserIdentity = Depends(require_permission(Permission.APPLICATION_UNDERWRITE)),
) -> ApplicationDetail:
    """
    Human-in-the-loop resumption: injects the determination into the paused workflow and resumes it,
    finalizing the file as **HUMAN_APPROVED** or **HUMAN_DENIED**. The adjudicating officer recorded is the
    signed-in underwriter.

    The caller (this user in this browser tab) must hold the file's checkout (one is taken implicitly when nobody holds it);
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
