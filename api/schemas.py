"""
Pydantic request / response contracts for the Autonomous Underwriting API.

These models are the public surface of the service. Internal dict records produced by
`loan_agent.db.ApplicationDatabase` are projected onto them in `api.services.applications`.
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from loan_agent.auth.roles import PAGES

LoanPurpose = Literal[
    "Home Improvement",
    "Debt Consolidation",
    "Medical Expenses",
    "Major Purchase",
    "Business",
    "Other",
]
LOAN_PURPOSES: List[str] = list(LoanPurpose.__args__)  # type: ignore[attr-defined]

HumanDecision = Literal["APPROVE", "DENY"]

LOAN_AMOUNT_MIN = 1000.0
LOAN_AMOUNT_MAX = 50000.0
ANNUAL_RATE = 0.12
TERM_MONTHS = 36


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
class ApplicationCreate(BaseModel):
    """Applicant Portal submission payload."""

    applicant_name: str = Field(..., min_length=1, max_length=120, examples=["Mrs. Some Name"])
    ssn_last_4: str = Field(..., pattern=r"^\d{4}$", examples=["1111"])
    stated_annual_income: float = Field(..., gt=0, examples=[96000.0])
    loan_amount_requested: float = Field(..., ge=LOAN_AMOUNT_MIN, le=LOAN_AMOUNT_MAX, examples=[15000.0])
    loan_purpose: LoanPurpose = Field(..., examples=["Home Improvement"])
    fcra_consent: bool = Field(
        True,
        description="Explicit FCRA/GLBA authorization for credit bureau inquiry and bank cash-flow review.",
    )

    @field_validator("applicant_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Borrower legal name is required.")
        return v


class Adjudication(BaseModel):
    model_config = ConfigDict(extra="allow")

    decision: Optional[str] = None
    officer: Optional[str] = None
    officer_user_id: Optional[str] = Field(None, description="User ID of the signed-in officer (human decisions).")
    reason: Optional[str] = None
    adjudicated_at: Optional[str] = None
    policy_citations: Optional[List[str]] = None
    adverse_action_reasons: Optional[List[str]] = None
    confidence_score: Optional[float] = None
    agent_notes: Optional[str] = None


class UserIdentity(BaseModel):
    """
    The signed-in caller, as established by `establish_user_identity()`: the authenticated principal (whatever the
    sign-in mechanism) plus, for the browser workbench, the tab it is calling from.
    """

    user_id: str = Field(..., examples=["uw.one"], description="Stable user ID of the signed-in user.")
    display_name: str = Field(..., examples=["Mrs. Some Name"])
    role: str = Field(..., examples=["UNDERWRITER"])
    role_label: str = Field(..., examples=["Loan Underwriter"])
    permissions: List[str] = Field(default_factory=list, examples=[["application:read", "application:underwrite"]])
    auth_method: str = Field(
        "password", examples=["password"],
        description="How the user signed in (future SSO / token mechanisms report their own method).",
    )
    password_change_required: bool = Field(
        False, description="True when the user must choose a new password: the session may only do that "
                           "(POST /api/v1/auth/password).",
    )
    home_path: Optional[str] = Field(None, examples=["/desk"], description="Workbench page to land on after sign-in.")
    pages: List[str] = Field(default_factory=list, examples=[["/desk"]], description="Workbench pages the user may open.")
    is_new_session: bool = Field(False, description="True when the server issued a new browser-tab session token.")
    tab_session: Optional[str] = Field(None, exclude=True)  # never serialized; echoed in X-Apex-Session-Id only

    @classmethod
    def from_principal(cls, principal: Any, tab_session: Optional[str] = None,
                       is_new_session: bool = False) -> "UserIdentity":
        return cls(
            user_id=principal.user_id,
            display_name=principal.display_name,
            role=principal.role.value,
            role_label=principal.role_label,
            permissions=sorted(p.value for p in principal.permissions),
            auth_method=principal.auth_method,
            password_change_required=principal.password_change_required,
            home_path=principal.home_path,
            pages=[path for path, _label, permission in PAGES if permission in principal.permissions],
            is_new_session=is_new_session,
            tab_session=tab_session,
        )

    @property
    def holder_key(self) -> str:
        """Review-checkout holder: the user in one browser tab (or the user as a whole for API clients)."""
        return f"{self.user_id}#{self.tab_session}" if self.tab_session else self.user_id

    @property
    def audit_actor(self) -> str:
        """How this caller is recorded in the regulatory audit trail."""
        return f"{self.display_name} ({self.user_id})"


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
class SignupRequest(BaseModel):
    """Self-service account creation — always the Loan Applicant role; no other role can be self-granted."""

    user_id: str = Field(..., min_length=1, max_length=64, examples=["applicant.one"])
    password: str = Field(..., min_length=1, max_length=256)
    display_name: str = Field("", max_length=120, examples=["Mrs. Some Name"])


class LoginRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64, examples=["uw.one"])
    password: str = Field(
        "", max_length=256,
        description="Your password.",
    )


class LoginResponse(BaseModel):
    identity: UserIdentity
    expires_at: str = Field(..., description="Latest end of the session (it also ends after a period of inactivity).")
    message: str


class TokenResponse(BaseModel):
    """OAuth2-style bearer token for API clients (send as `Authorization: Bearer <access_token>`)."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(..., description="Seconds until the token expires at the latest.")
    password_change_required: bool = False


class PasswordChangeRequest(BaseModel):
    current_password: str = Field("", max_length=256, description="Your current password.")
    new_password: str = Field(..., max_length=256)


class SignInOption(BaseModel):
    id: str = Field(..., examples=["local"])
    type: Literal["password", "redirect"] = Field(
        ..., description="`password`: POST user ID + password to `url`. `redirect`: navigate to `url` (SSO)."
    )
    label: str = Field(..., examples=["User ID & password"])
    url: str


class CheckoutInfo(BaseModel):
    """An active review checkout: the application is open in exactly one reviewer's desk."""

    application_id: str
    checked_out_by: str = Field(
        ..., examples=["another session"],
        description="Your own label when you hold the checkout; otherwise always \"another session\" (the holder "
                    "is never disclosed to other sessions).",
    )
    held_by_you: bool = Field(False, description="True when the caller (this user in this browser tab) holds it.")
    checked_out_at: str
    expires_at: str = Field(..., description="Lease end; renewed by heartbeats while the review stays open.")
    state: Literal["ACTIVE", "DECIDING"] = "ACTIVE"
    ttl_seconds: int = Field(..., description="Lease length granted by each checkout / heartbeat.")
    heartbeat_seconds: int = Field(..., description="How often an open review should renew its checkout.")


class CheckoutReleaseResponse(BaseModel):
    application_id: str
    released: bool
    message: str


class ApplicationSummary(BaseModel):
    """Queue / archive listing projection (no raw graph state)."""

    application_id: str
    applicant_name: str
    ssn_masked: str = Field(..., examples=["••••-1111"])
    stated_annual_income: float
    loan_amount_requested: float
    loan_purpose: str
    fcra_consent: bool
    status: str
    stage: Literal["NEW", "PROCESSING", "PENDING_REVIEW", "ADJUDICATED"]
    submitted_by: Optional[str] = Field(None, description="User ID of the self-service applicant, if any.")
    submitted_at: Optional[str] = None
    updated_at: Optional[str] = None
    estimated_monthly_payment: Optional[float] = None
    post_loan_dti: Optional[float] = None
    underwriting_decision: Optional[str] = None
    adjudicator_type: Optional[Literal["AI_AGENT", "HUMAN_OFFICER"]] = None
    adjudication: Optional[Adjudication] = None
    checkout: Optional[CheckoutInfo] = Field(
        None, description="Set while a reviewer has the file open in the Underwriter Desk (PENDING_REVIEW only)."
    )


class ApplicationDetail(ApplicationSummary):
    graph_state: Optional[Dict[str, Any]] = None


class NextReviewResponse(BaseModel):
    """Get Work: the file now checked out to the caller, or nothing when no file is available."""

    application: Optional[ApplicationSummary] = None
    checkout: Optional[CheckoutInfo] = None
    message: str


class ApplicationCreateResponse(BaseModel):
    application: ApplicationDetail
    message: str


class SlaInfo(BaseModel):
    """Apex Financial's published decision commitment: a determination within 3 business days of submission."""

    business_days: int = Field(..., examples=[3])
    due_at: Optional[str] = Field(None, description="Expected decision time; not set once a decision is recorded.")
    business_days_remaining: Optional[int] = Field(
        None, description="Business days until due_at (negative once overdue); not set once decided."
    )
    state: Literal["ON_TRACK", "DUE_SOON", "OVERDUE", "DECIDED"]
    decided_within_sla: Optional[bool] = Field(None, description="Set once a decision is recorded.")
    message: str = Field(..., examples=["On track: a decision is expected by 2026-01-08 09:00:00 (our "
                                        "3-business-day target)."])


class MyApplicationStatus(BaseModel):
    """
    What a Loan Applicant sees about their own application: no internal adjudicator memos, policy citations, or
    review-checkout details — just enough to track status, plus the decision SLA. ECOA adverse action reasons ARE
    included on a denial (the law requires disclosing them to the applicant).
    """

    application_id: str
    applicant_name: str
    loan_amount_requested: float
    loan_purpose: str
    submitted_at: Optional[str] = None
    updated_at: Optional[str] = None
    status_label: str = Field(..., examples=["Under Review"])
    main_status: str = Field(..., examples=["pending review"])
    decision_code: Optional[str] = Field(None, examples=["AUTO_APPROVED"])
    decided_at: Optional[str] = None
    adverse_action_reasons: Optional[List[str]] = None
    sla: SlaInfo


class AuditEvent(BaseModel):
    id: int
    application_id: str
    timestamp: str
    event_type: str
    actor: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    details: Optional[Any] = None


class StatusGroup(BaseModel):
    """One of the five administrator-facing main statuses with its sub-status breakdown."""

    status: str = Field(..., examples=["rejected"])
    label: str = Field(..., examples=["Rejected"])
    count: int
    sub_statuses: Dict[str, int] = Field(default_factory=dict, examples=[{"AUTO_DENIED": 2, "HUMAN_DENIED": 1}])


class QueueStats(BaseModel):
    new: int
    processing: int
    pending_review: int
    adjudicated: int
    total: int
    by_status: Dict[str, int] = Field(
        default_factory=dict,
        description="Exact record counts grouped by persisted status (e.g. NEW, PENDING_REVIEW, AUTO_APPROVED).",
    )
    main_statuses: List[StatusGroup] = Field(
        default_factory=list,
        description="Counts rolled up into the five main statuses: new, pending processing, pending review, approved, rejected.",
    )


# ---------------------------------------------------------------------------
# Workflow execution & human-in-the-loop
# ---------------------------------------------------------------------------
class WorkflowItem(BaseModel):
    application_id: str
    applicant_name: str
    status: str
    decision: Optional[str] = None
    error: Optional[str] = None


class WorkflowRunRequest(BaseModel):
    limit: Optional[int] = Field(None, ge=1, description="Maximum number of NEW applications to process.")
    dry_run: bool = Field(False, description="Preview eligible applications without executing the pipeline.")


class WorkflowRunSummary(BaseModel):
    found: int
    selected: int
    dry_run: bool
    approved: int
    rejected: int
    escalated: int
    errors: int
    items: List[WorkflowItem]


class WorkflowRun(BaseModel):
    """A logged execution of the initiate_workflow job."""

    id: int
    trigger_source: str = Field(..., examples=["SCHEDULER", "CLI", "API"])
    status: str = Field(..., examples=["REQUESTED", "RUNNING", "COMPLETED", "FAILED"])
    requested_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    found: int = 0
    selected: int = 0
    approved: int = 0
    rejected: int = 0
    escalated: int = 0
    errors: int = 0
    error_message: Optional[str] = None


class WorkflowRunAccepted(BaseModel):
    """Fire-and-forget acknowledgement for a requested initiate_workflow run."""

    run_id: int
    status: str
    found: int = Field(..., description="Number of NEW applications at the time the run was requested.")
    message: str


class ScheduleConfig(BaseModel):
    interval_minutes: int = Field(..., ge=1, le=1440, description="Expected gap between two initiate_workflow runs.")
    scheduler_enabled: bool = Field(..., description="Whether the service itself fires the job on that cadence.")
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class ScheduleConfigUpdate(BaseModel):
    """The change is recorded against the signed-in user (`updated_by`)."""

    interval_minutes: Optional[int] = Field(None, ge=1, le=1440)
    scheduler_enabled: Optional[bool] = None


class SchedulerStatus(BaseModel):
    config: ScheduleConfig
    health: Literal["NEVER_RUN", "RUNNING", "HEALTHY", "OVERDUE"]
    message: str
    last_run: Optional[WorkflowRun] = None
    seconds_since_last_run: Optional[int] = None
    next_due_at: Optional[str] = None


class IntakePartner(BaseModel):
    partner_id: str
    name: str
    inbox_dir: str
    outbox_dir: str
    pending_files: int = Field(..., description="`*.csv` files currently waiting in the partner's inbox.")


class IntakeStatus(BaseModel):
    """Partner batch-intake monitor: configuration, liveness and approved partners."""

    enabled: bool = Field(..., description="batch_intake.ini `enabled`.")
    monitoring: bool = Field(..., description="Whether this service process is currently monitoring the inboxes.")
    spec_version: str
    columns: List[str]
    root_dir: str
    inbox_dir: str
    poll_seconds: float
    settle_seconds: float
    max_rows: int
    max_file_bytes: int
    last_scan_at: Optional[str] = None
    partners: List[IntakePartner]


class IntakeBatch(BaseModel):
    """One batch file received from a partner."""

    id: int
    partner_id: str
    file_name: str
    status: str = Field(..., examples=["ACCEPTED", "PARTIALLY_ACCEPTED", "FLAGGED", "REJECTED", "DUPLICATE_FILE",
                                       "FAILED", "INTERRUPTED", "RECEIVED"])
    received_at: str
    processed_at: Optional[str] = None
    total_rows: int = 0
    accepted_rows: int = 0
    flagged_rows: int = 0
    error_message: Optional[str] = None
    stored_path: Optional[str] = None
    results_path: Optional[str] = None
    sha256: Optional[str] = None
    file_bytes: int = 0


class IntakeRow(BaseModel):
    row_number: int
    partner_reference: Optional[str] = None
    status: Literal["ACCEPTED", "FLAGGED"]
    application_id: Optional[str] = None
    errors: List[str] = Field(default_factory=list)


class IntakeBatchDetail(IntakeBatch):
    rows: List[IntakeRow]


class IntakeScanResult(BaseModel):
    scanned_at: str
    batches: List[IntakeBatch]


class WorkflowScheduledResponse(BaseModel):
    application_id: str
    status: str
    scheduled: bool
    message: str


class HumanDecisionRequest(BaseModel):
    """
    The adjudicating officer recorded on the determination is the signed-in underwriter; it cannot be supplied by
    the caller. The caller (user + browser tab) must hold the file's checkout (taken implicitly if nobody does).
    """

    decision: HumanDecision
    notes: str = Field(..., description="Mandatory credit officer justification memorandum.")

    @field_validator("notes")
    @classmethod
    def _notes_required(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("Mandatory: enter the underwriter justification memorandum before adjudicating.")
        return v.strip()


# ---------------------------------------------------------------------------
# Review dossier (Underwriter Desk modal)
# ---------------------------------------------------------------------------
class FinancialRatios(BaseModel):
    fico_score: int = 0
    has_active_bankruptcy: bool = False
    total_delinquencies_12m: int = 0
    nsf_count_90d: int = 0
    bureau_monthly_debt: float = 0.0
    undisclosed_monthly_debt: float = 0.0
    estimated_new_loan_payment: float = 0.0
    total_post_loan_debt: float = 0.0
    verified_monthly_income: float = 0.0
    stated_monthly_income: float = 0.0
    income_variance_pct: float = 0.0
    post_loan_dti: float = 0.0
    avg_ending_balance: float = 0.0
    payment_cushion_ratio: float = 0.0


class RatioRow(BaseModel):
    metric: str
    value: str
    benchmark: str


class RatiosResponse(BaseModel):
    ratios: FinancialRatios
    rows: List[RatioRow]
    source: Literal["GRAPH_STATE", "RECALCULATED"]


class RatioRecalculateRequest(BaseModel):
    classifications: Dict[str, str] = Field(
        ...,
        description="Map of tx_id -> human-readable classification label (see dossier.bank.transactions[].options).",
    )


class TradelineRow(BaseModel):
    creditor: str
    account_type: str
    monthly_payment: float
    delinquencies: str
    standing: str


class PublicRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Optional[str] = None
    status: Optional[str] = None
    filed_date: Optional[str] = None


class CreditSection(BaseModel):
    fico_score: int
    bureau_monthly_debt: float
    public_records: List[PublicRecord]
    tradelines: List[TradelineRow]


class BankTransaction(BaseModel):
    tx_id: str
    date: Optional[str] = None
    description: str
    amount: float
    flow: Literal["CREDIT", "DEBIT"]
    classification: str
    ai_reasoning: Optional[str] = None
    options: List[str]


class BankSection(BaseModel):
    bank_name: Optional[str] = None
    account_mask: Optional[str] = None
    statement_period_months: float = 3.0
    avg_eom_balance: float
    total_inflows: float
    total_outflows: float
    transactions: List[BankTransaction]
    classified_by_ai: bool


class AIAssessment(BaseModel):
    underwriting_decision: str
    confidence_score: float
    confidence_pct: str
    underwriter_memo: str
    policy_citations: List[str]
    adverse_action_reasons: List[str]
    bank_analyst_summary: str
    execution_log: List[str]
    error_message: Optional[str] = None


class LoanSizing(BaseModel):
    loan_amount_requested: float
    monthly_payment: Optional[float]
    annual_rate: float = ANNUAL_RATE
    term_months: int = TERM_MONTHS


class Dossier(BaseModel):
    application: ApplicationSummary
    stated_monthly_income: float
    sizing: LoanSizing
    ai_assessment: AIAssessment
    credit: CreditSection
    bank: BankSection
    ratios: RatiosResponse


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
class Persona(BaseModel):
    key: str
    label: str
    applicant_name: str
    ssn_last_4: str
    stated_annual_income: float
    loan_amount_requested: float
    loan_purpose: str


class InstallmentQuote(BaseModel):
    loan_amount: float
    annual_rate: float
    term_months: int
    monthly_payment: float


class PolicyDocument(BaseModel):
    markdown: str
    html: str


class SystemInfo(BaseModel):
    service: str = "Apex Financial — Autonomous Underwriting API"
    version: str
    model: str
    llm_base_url: str
    llm_configured: bool
    storage_mode: str
    loan_purposes: List[str]
    loan_amount_min: float = LOAN_AMOUNT_MIN
    loan_amount_max: float = LOAN_AMOUNT_MAX
    annual_rate: float = ANNUAL_RATE
    term_months: int = TERM_MONTHS
    stats: QueueStats


class Message(BaseModel):
    detail: str
