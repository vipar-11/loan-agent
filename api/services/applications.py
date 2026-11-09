"""
Application lifecycle services: submission, identifiers, listing, projections, queue statistics.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from api.schemas import (
    ApplicationCreate,
    ApplicationDetail,
    ApplicationSummary,
    CheckoutInfo,
    QueueStats,
    StatusGroup,
)
from loan_agent.calculator import calculate_monthly_installment
from loan_agent.db import ApplicationDatabase, ApplicationIdConflict

# Status vocabulary ---------------------------------------------------------
NEW_STATUSES = {"NEW"}
PROCESSING_STATUSES = {"PENDING_PROCESSING"}
PENDING_REVIEW_STATUSES = {"PENDING_REVIEW"}
OPEN_STATUSES = NEW_STATUSES | PROCESSING_STATUSES | PENDING_REVIEW_STATUSES

# Friendly list filters accepted by the API on top of `db.normalize_status_filter` vocabulary.
STAGE_FILTERS = {"new", "processing", "pending_review", "adjudicated", "all"}


def stage_for(status: Optional[str]) -> str:
    s = (status or "").upper()
    if s in NEW_STATUSES:
        return "NEW"
    if s in PROCESSING_STATUSES:
        return "PROCESSING"
    if s in PENDING_REVIEW_STATUSES:
        return "PENDING_REVIEW"
    return "ADJUDICATED"


def is_adjudicated(status: Optional[str]) -> bool:
    return stage_for(status) == "ADJUDICATED"


def mask_ssn(ssn_last_4: Any) -> str:
    return f"••••-{ssn_last_4}"


def generate_unique_app_id(db: ApplicationDatabase, attempt: int = 0) -> str:
    """
    Sequential reference ID: APP-<YYYY>-<NNN>, with a -<HHMMSS>[-<n>] suffix when that number is already taken.
    Uniqueness is guaranteed by create_application(), which inserts without overwriting and retries on conflict.
    """
    now = datetime.now()
    count = db.count_applications() + 1
    candidate = f"APP-{now.strftime('%Y')}-{count:03d}"
    if attempt == 0 and not db.get_application(candidate):
        return candidate
    candidate = f"{candidate}-{now.strftime('%H%M%S')}"
    return candidate if attempt <= 1 else f"{candidate}-{attempt}"


MAX_ID_ATTEMPTS = 10


def create_application(
    db: ApplicationDatabase,
    payload: ApplicationCreate,
    actor: str = "APPLICANT",
    details: str = "Application submitted via Applicant Portal; queued for approval workflow initiation.",
) -> Dict[str, Any]:
    """
    Persists a NEW application and writes the APPLICATION_SUBMITTED audit event (`actor` / `details` identify the
    submission channel). Never overwrites an existing record: concurrent submissions (portal, partner batch intake)
    that race for the same sequential ID are retried with a fresh one.
    """
    record = {
        "applicant_name": payload.applicant_name,
        "ssn_last_4": payload.ssn_last_4,
        "stated_annual_income": float(payload.stated_annual_income),
        "loan_amount_requested": float(payload.loan_amount_requested),
        "loan_purpose": payload.loan_purpose,
        "fcra_consent": bool(payload.fcra_consent),
        "status": "NEW",
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "graph_state": None,
        "adjudication": None,
    }
    for attempt in range(MAX_ID_ATTEMPTS):
        app_id = generate_unique_app_id(db, attempt)
        try:
            return db.insert_new_application(
                {**record, "application_id": app_id}, actor=actor, event_type="APPLICATION_SUBMITTED", details=details
            )
        except ApplicationIdConflict:
            continue
    raise RuntimeError("Could not allocate a unique application ID; please retry.")


def list_applications(db: ApplicationDatabase, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lists applications. `status` accepts either a stage filter
    (new | processing | pending_review | adjudicated | all) or any status vocabulary
    understood by `loan_agent.db.normalize_status_filter` (approved, rejected, ...).
    """
    key = (status or "all").strip().lower().replace("-", "_").replace(" ", "_")
    if key == "all":
        return db.list_applications()
    if key == "adjudicated":
        return [a for a in db.list_applications() if is_adjudicated(a.get("status"))]
    if key == "processing":
        return db.list_applications(status="pending processing")
    if key == "pending_review":
        return db.list_applications(status="pending review")
    return db.list_applications(status=status)


def queue_stats(db: ApplicationDatabase) -> QueueStats:
    apps = db.list_applications()
    stages = [stage_for(a.get("status")) for a in apps]
    by_status: Dict[str, int] = {}
    for a in apps:
        key = (a.get("status") or "UNKNOWN").upper()
        by_status[key] = by_status.get(key, 0) + 1
    return QueueStats(
        new=stages.count("NEW"),
        processing=stages.count("PROCESSING"),
        pending_review=stages.count("PENDING_REVIEW"),
        adjudicated=stages.count("ADJUDICATED"),
        total=len(apps),
        by_status=dict(sorted(by_status.items())),
        main_statuses=[StatusGroup(**g) for g in db.status_summary()],
    )


def _adjudicator_type(record: Dict[str, Any]) -> Optional[str]:
    if not is_adjudicated(record.get("status")):
        return None
    adj = record.get("adjudication") or {}
    decision = str(adj.get("decision") or record.get("status") or "")
    officer = str(adj.get("officer") or "")
    if "HUMAN" in decision.upper() or (officer and "AI AGENT" not in officer.upper()):
        return "HUMAN_OFFICER"
    return "AI_AGENT"


def to_summary(record: Dict[str, Any], checkout: Optional[CheckoutInfo] = None) -> ApplicationSummary:
    """`checkout` is only surfaced while the file is awaiting review (a stale row is never shown)."""
    state = record.get("graph_state") or {}
    if stage_for(record.get("status")) != "PENDING_REVIEW":
        checkout = None
    loan_amount = float(record.get("loan_amount_requested") or 0.0)
    return ApplicationSummary(
        application_id=record["application_id"],
        applicant_name=record["applicant_name"],
        ssn_masked=mask_ssn(record.get("ssn_last_4", "")),
        stated_annual_income=float(record.get("stated_annual_income") or 0.0),
        loan_amount_requested=loan_amount,
        loan_purpose=record.get("loan_purpose") or "General",
        fcra_consent=bool(record.get("fcra_consent")),
        status=record.get("status") or "NEW",
        stage=stage_for(record.get("status")),  # type: ignore[arg-type]
        submitted_at=record.get("submitted_at"),
        updated_at=record.get("updated_at"),
        estimated_monthly_payment=calculate_monthly_installment(loan_amount) if loan_amount > 0 else None,
        post_loan_dti=state.get("post_loan_dti"),
        underwriting_decision=state.get("underwriting_decision"),
        adjudicator_type=_adjudicator_type(record),  # type: ignore[arg-type]
        adjudication=record.get("adjudication"),
        checkout=checkout,
    )


def to_detail(record: Dict[str, Any], checkout: Optional[CheckoutInfo] = None) -> ApplicationDetail:
    return ApplicationDetail(**to_summary(record, checkout).model_dump(), graph_state=record.get("graph_state"))
