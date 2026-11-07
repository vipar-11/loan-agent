"""
/api/v1 reference & system endpoints: health, system info, demo personas, installment quotes, policy document.
"""
import os
from typing import List

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_db
from api.schemas import (
    ANNUAL_RATE,
    LOAN_AMOUNT_MAX,
    LOAN_AMOUNT_MIN,
    LOAN_PURPOSES,
    TERM_MONTHS,
    InstallmentQuote,
    Persona,
    PolicyDocument,
    SystemInfo,
)
from api.services import applications as app_service
from api.services.policy import load_policy_html, load_policy_markdown
from loan_agent.calculator import calculate_monthly_installment
from loan_agent.config import load_db_config
from loan_agent.db import ApplicationDatabase
from loan_agent.mock_apis import APPLICANT_PROFILES

router = APIRouter(prefix="/api/v1", tags=["Reference & System"])

API_VERSION = "1.0.0"


@router.get("/health", summary="Liveness probe")
def health() -> dict:
    return {"status": "ok"}


@router.get("/system", response_model=SystemInfo, summary="Runtime configuration & queue overview")
def system_info(db: ApplicationDatabase = Depends(get_db)) -> SystemInfo:
    return SystemInfo(
        version=API_VERSION,
        model=os.environ.get("LLM_MODEL", "openai/gpt-oss-120b"),
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
        llm_configured=bool(
            os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY") or os.environ.get("OPENAI_API_KEY")
        ),
        storage_mode=load_db_config().mode,
        loan_purposes=LOAN_PURPOSES,
        stats=app_service.queue_stats(db),
    )


@router.get("/personas", response_model=List[Persona], summary="Demo borrower personas")
def list_personas() -> List[Persona]:
    return [
        Persona(
            key=key,
            label=p.get("persona_label", p["applicant_name"]),
            applicant_name=p["applicant_name"],
            ssn_last_4=p["ssn_last_4"],
            stated_annual_income=float(p["stated_annual_income"]),
            loan_amount_requested=float(p["loan_amount_requested"]),
            loan_purpose=p["loan_purpose"],
        )
        for key, p in APPLICANT_PROFILES.items()
    ]


@router.get("/calculator/installment", response_model=InstallmentQuote, summary="Fixed-rate installment quote")
def installment_quote(
    loan_amount: float = Query(..., ge=LOAN_AMOUNT_MIN, le=LOAN_AMOUNT_MAX),
) -> InstallmentQuote:
    return InstallmentQuote(
        loan_amount=loan_amount,
        annual_rate=ANNUAL_RATE,
        term_months=TERM_MONTHS,
        monthly_payment=calculate_monthly_installment(loan_amount, ANNUAL_RATE, TERM_MONTHS),
    )


@router.get("/policy", response_model=PolicyDocument, summary="Underwriting policy document (policy_v1.md)")
def policy_document() -> PolicyDocument:
    md = load_policy_markdown()
    return PolicyDocument(markdown=md, html=load_policy_html(md))
