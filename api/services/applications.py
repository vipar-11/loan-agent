"""
Application lifecycle services: submission, identifiers, listing, projections, queue statistics.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from api.schemas import (
    ApplicationCreate,
    ApplicationDetail,
    ApplicationSummary,
    CheckoutInfo,
    MyApplicationStatus,
    QueueStats,
    SlaInfo,
    StatusGroup,
)
from loan_agent import sla as sla_engine
from loan_agent.calculator import calculate_monthly_installment
from loan_agent.db import (
    ApplicationDatabase,
    ApplicationIdConflict,
    ApplicationQuery,
    main_status_for,
    normalize_status_filter,
)

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
    submitted_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Persists a NEW application and writes the APPLICATION_SUBMITTED audit event (`actor` / `details` identify the
    submission channel). Never overwrites an existing record: concurrent submissions (portal, partner batch intake)
    that race for the same sequential ID are retried with a fresh one.

    `submitted_by` is the owning self-service Loan Applicant's user ID (see GET /api/v1/applications/mine); it is
    None for channels with no signed-in applicant account (partner batch intake, admin-triggered submissions).
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
        "submitted_by": submitted_by,
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


def stage_criteria(status: Optional[str]) -> Dict[str, Any]:
    """
    SQL status criteria for a listing filter: either a stage (new | processing | pending_review | adjudicated |
    all) or any status vocabulary understood by `loan_agent.db.normalize_status_filter` (approved, rejected, ...).
    Returns keyword arguments for `ApplicationQuery` (statuses / exclude_statuses).
    """
    key = (status or "all").strip().lower().replace("-", "_").replace(" ", "_")
    if key == "all":
        return {}
    if key == "adjudicated":
        return {"exclude_statuses": sorted(OPEN_STATUSES)}
    if key == "processing":
        return {"statuses": normalize_status_filter("pending processing")}
    if key == "pending_review":
        return {"statuses": normalize_status_filter("pending review")}
    return {"statuses": normalize_status_filter(status)}


# ---------------------------------------------------------------------------
# Queue filtering & ordering (Underwriter Desk: cherry-pick by loan metadata; work the oldest files first).
# Every criterion becomes part of the SQL query (loan_agent.db.ApplicationQuery) — nothing is filtered in memory.
# ---------------------------------------------------------------------------
SORT_ORDERS = ("newest", "oldest")


@dataclass(frozen=True)
class ApplicationFilters:
    """
    Metadata filters for queue listings, as accepted by the API; every criterion that is set must match (AND).
    `decided_by` is a user ID matched against the officer who recorded the determination — never the display
    name, which is not unique.
    """

    q: Optional[str] = None                 # applicant name or reference number, case-insensitive substring
    loan_purpose: Optional[str] = None
    min_amount: Optional[float] = None
    max_amount: Optional[float] = None
    submitted_from: Optional[date] = None   # inclusive, by calendar day
    submitted_to: Optional[date] = None     # inclusive, by calendar day
    decided_by: Optional[str] = None

    def validate(self) -> None:
        """Raises ValueError for contradictory ranges (reported to API callers as 422)."""
        if self.min_amount is not None and self.max_amount is not None and self.min_amount > self.max_amount:
            raise ValueError("min_amount cannot be greater than max_amount.")
        if self.submitted_from and self.submitted_to and self.submitted_from > self.submitted_to:
            raise ValueError("submitted_from cannot be later than submitted_to.")

    def criteria(self) -> Dict[str, Any]:
        """ApplicationQuery keyword arguments. Calendar days become timestamp bounds: [from 00:00, to+1 00:00)."""
        return {
            "search": self.q,
            "loan_purpose": self.loan_purpose,
            "min_amount": self.min_amount,
            "max_amount": self.max_amount,
            "submitted_from": f"{self.submitted_from.isoformat()} 00:00:00" if self.submitted_from else None,
            "submitted_before": f"{(self.submitted_to + timedelta(days=1)).isoformat()} 00:00:00"
                                if self.submitted_to else None,
            "decided_by": self.decided_by,
        }


def query_applications(
    db: ApplicationDatabase,
    status: Optional[str] = None,
    filters: Optional[ApplicationFilters] = None,
    sort: str = "newest",
    submitted_by: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    One database query: stage / status + metadata filters + ordering by submission time — "oldest" first in, first
    out (the pending review queue works the longest-waiting files first) or "newest" first; ties by reference number.
    """
    if sort not in SORT_ORDERS:
        raise ValueError(f"sort must be one of: {', '.join(SORT_ORDERS)}.")
    return db.query_applications(ApplicationQuery(
        **stage_criteria(status),
        **(filters.criteria() if filters else {}),
        submitted_by=submitted_by,
        oldest_first=(sort == "oldest"),
    ))


def list_applications(db: ApplicationDatabase, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lists every application, newest first. See `stage_criteria` for the accepted `status` values."""
    return query_applications(db, status)


def queue_stats(db: ApplicationDatabase) -> QueueStats:
    """Counts per stage / status via one GROUP BY query (the Underwriter Desk polls this every few seconds)."""
    by_status = db.count_by_status()  # exact status -> count, keys upper-cased
    stages: Dict[str, int] = {"NEW": 0, "PROCESSING": 0, "PENDING_REVIEW": 0, "ADJUDICATED": 0}
    for status_name, count in by_status.items():
        stages[stage_for(status_name)] += count
    return QueueStats(
        new=stages["NEW"],
        processing=stages["PROCESSING"],
        pending_review=stages["PENDING_REVIEW"],
        adjudicated=stages["ADJUDICATED"],
        total=sum(by_status.values()),
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
        submitted_by=record.get("submitted_by"),
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


# ---------------------------------------------------------------------------
# "My Applications" — the self-service Loan Applicant's own submissions (GET /api/v1/applications/mine)
# ---------------------------------------------------------------------------
STATUS_LABELS: Dict[str, str] = {
    "new": "Received", "pending processing": "Under Review", "pending review": "Under Review",
    "approved": "Approved", "rejected": "Denied",
}


def list_own_applications(db: ApplicationDatabase, user_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Every application owned by `user_id` (never another user's), newest first, in one database query. Accepts the
    same friendly `status` filters as the queues (see `stage_criteria`) — e.g. `processing`, `adjudicated`.
    """
    return query_applications(db, status, submitted_by=user_id)


def to_my_status(record: Dict[str, Any], now: Optional[datetime] = None) -> MyApplicationStatus:
    main_status = main_status_for(record.get("status"))
    adjudication = record.get("adjudication") or {}
    graph_state = record.get("graph_state") or {}
    decided_at = adjudication.get("adjudicated_at") if main_status in ("approved", "rejected") else None
    decision_code = record.get("status") if decided_at else None
    adverse = (adjudication.get("adverse_action_reasons") or graph_state.get("adverse_action_reasons")
              if main_status == "rejected" else None)

    status = sla_engine.evaluate(record["submitted_at"], decided_at=decided_at, now=now)
    return MyApplicationStatus(
        application_id=record["application_id"],
        applicant_name=record["applicant_name"],
        loan_amount_requested=float(record.get("loan_amount_requested") or 0.0),
        loan_purpose=record.get("loan_purpose") or "General",
        submitted_at=record.get("submitted_at"),
        updated_at=record.get("updated_at"),
        status_label=STATUS_LABELS.get(main_status, "Under Review"),
        main_status=main_status,
        decision_code=decision_code,
        decided_at=decided_at,
        adverse_action_reasons=adverse or None,
        sla=SlaInfo(**status.__dict__),
    )
