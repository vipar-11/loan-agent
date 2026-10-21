"""
graph_workflow.py
Implements the 6-node LangGraph workflow for Unsecured Personal Loan Underwriting.

Teaching Highlights Included:
1. Provider-Agnostic LLM Setup: Uses ChatOpenAI with configurable base_url (defaults to Groq gpt-oss).
2. Zero Policy Hardcoding: Prompts contain pure facts; all rules/thresholds come dynamically from policy_v1.md.
3. Standard LangChain vs. LCEL:
   - Node 3 teaches Standard Invocation (`prompt.format_messages()` + `structured_llm.invoke()`).
   - Node 5 teaches LCEL Pipe Invocation (`chain = prompt | structured_llm`).
4. Conditional Routing Styles (`list` vs `dict`):
   - `route_after_consent` uses a `list` path map for parallel Fan-Out.
   - `route_after_underwriter` returns symbolic intents ("NEEDS_HUMAN_REVIEW", "READY_TO_FINALIZE")
     mapped via an explicit `dict` to target node names.
5. Production Error Handling: Combines LangGraph `RetryPolicy` with graceful `try/except` fallback
   that routes unexpected failures safely to `human_review_node` (ESCALATE).
"""
import os
import json
from pathlib import Path
from typing import Dict, Any, List, Literal
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import RetryPolicy

from loan_agent.state import LoanApplicationState
from loan_agent.schema import (
    BankStatementAnalysisOutput,
    UnderwritingOutput,
)
from loan_agent.mock_apis import (
    fetch_experian_credit_report,
    fetch_plaid_bank_statement,
    prefilter_underwriting_transactions,
)

# Load .env file automatically if present
load_dotenv()

# ---------------------------------------------------------------------------
# HELPER: Load Underwriting Policy Markdown & Initialize OpenAI-Compatible LLM
# ---------------------------------------------------------------------------
def load_policy_document() -> str:
    candidates = [
        # Path(__file__).parent / "policy_v1.md",
        # Path(__file__).resolve().parent.parent.parent / "policy_v1.md",
        Path.cwd() / "policy_v1.md",
    ]
    for p in candidates:
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError("Underwriting policy document 'policy_v1.md' not found.")


def get_llm(
    model_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ChatOpenAI:
    """
    Initializes a provider-agnostic ChatOpenAI client.
    Defaults to Groq's OpenAI-compatible endpoint with 'openai/gpt-oss-120b'.
    """
    resolved_model = model_name or os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")
    resolved_base_url = base_url or os.environ.get(
        "LLM_BASE_URL", "https://api.groq.com/openai/v1"
    )
    resolved_api_key = (
        api_key
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("GROQ_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )

    if not resolved_api_key:
        raise ValueError(
            "API key is missing. Set GROQ_API_KEY, OPENAI_API_KEY, or LLM_API_KEY "
            "in your environment or the Streamlit sidebar."
        )

    return ChatOpenAI(
        model=resolved_model,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        temperature=0.0,
    )


# ---------------------------------------------------------------------------
# NODE 1: DETERMINISTIC CONSENT GATEKEEPER (Zero Hardcoded Policy Strings)
# ---------------------------------------------------------------------------
def validate_consent_node(state: LoanApplicationState) -> Dict[str, Any]:
    """
    Technical Precondition Gatekeeper:
    Verifies explicit applicant consent before invoking external data APIs.
    Does not hardcode policy section or rule numbers.
    """
    logs = list(state.get("execution_log") or [])
    if not state.get("fcra_consent", False):
        logs.append(
            "❌ [Node 1: Consent Gatekeeper] Applicant consent flag is False. Halting external API calls."
        )
        return {
            "underwriting_decision": "REJECT_NO_CONSENT",
            "policy_citations": ["MANDATORY_CONSENT_PRECONDITION"],
            "adverse_action_reasons": [
                "Application cannot be processed without explicit applicant authorization to access credit and bank records."
            ],
            "underwriter_memo": "System Precondition Halt: Applicant did not provide consent to pull credit bureau or bank data.",
            "final_status": "REJECTED_NO_CONSENT",
            "execution_log": logs,
        }

    logs.append(
        "✅ [Node 1: Consent Gatekeeper] Applicant consent verified. Triggering parallel API pulls."
    )
    return {"execution_log": logs}


def route_after_consent(
    state: LoanApplicationState,
) -> List[str] | Literal["finalize_application_node"]:
    """
    Routing Style #1 (Direct Node List):
    Returns a list of destination node names to trigger parallel Fan-Out,
    or a single node name if consent was not provided.
    """
    if not state.get("fcra_consent", False):
        return "finalize_application_node"
    return ["pull_credit_bureau_node", "pull_raw_bank_transactions_node"]


# ---------------------------------------------------------------------------
# NODE 2A & 2B: PARALLEL EXTERNAL DATA HARVESTERS (With Exception Capture)
# ---------------------------------------------------------------------------
def pull_credit_bureau_node(state: LoanApplicationState) -> Dict[str, Any]:
    """Fetches Experian Credit Report JSON (FICO, public records, tradelines)."""
    try:
        credit_data = fetch_experian_credit_report(state["ssn_last_4"])
        return {"credit_report": credit_data}
    except Exception as exc:
        return {
            "credit_report": None,
            "error_message": f"Credit Bureau API Error: {str(exc)}",
        }


def pull_raw_bank_transactions_node(state: LoanApplicationState) -> Dict[str, Any]:
    """Fetches 90-day Plaid bank ledger and applies deterministic noise pre-filtering."""
    try:
        raw_bank = fetch_plaid_bank_statement(state["ssn_last_4"])
        filtered_txs = prefilter_underwriting_transactions(raw_bank["transactions"])
        return {
            "bank_statement_raw": raw_bank,
            "prefiltered_transactions": filtered_txs,
        }
    except Exception as exc:
        return {
            "bank_statement_raw": None,
            "prefiltered_transactions": [],
            "error_message": f"Bank Transactions API Error: {str(exc)}",
        }


# ---------------------------------------------------------------------------
# NODE 3: AGENT #1 - BANK STATEMENT ANALYZER (Teaches Standard LangChain Invocation)
# ---------------------------------------------------------------------------
def bank_statement_analyzer_agent(state: LoanApplicationState) -> Dict[str, Any]:
    """
    Single Batch LLM Call using STANDARD LangChain invocation:
    `messages = prompt.format_messages(...)` -> `structured_llm.invoke(messages)`
    If an upstream API failed or the LLM throws an error, records `error_message` for safe escalation.
    """
    logs = list(state.get("execution_log") or [])

    # Check if upstream parallel API nodes encountered an error
    if state.get("error_message") or not state.get("credit_report") or not state.get("bank_statement_raw"):
        err = state.get("error_message") or "Missing upstream credit bureau or bank data."
        logs.append(f"⚠️ [Node 3: Bank Analyzer Agent] Skipping due to upstream error: {err}")
        return {"execution_log": logs}

    try:
        llm = get_llm()
        structured_llm = llm.with_structured_output(BankStatementAnalysisOutput)

        system_prompt = """You are a Senior Financial Data Analyst for an automated underwriting pipeline.
Your job is to classify 90 days of pre-filtered bank transactions into strict underwriting categories
and reconcile debt outflows against the applicant's known credit bureau tradelines.

CRITICAL RULES:
1. DO NOT perform math or calculate ratios. Only classify each transaction in `categorized_transactions`.
2. INCOME CLASSIFICATION:
   - Tag recurring employer direct deposits / wages as `VERIFIED_PAYROLL_INCOME`.
   - Tag internal account transfers (e.g., 'XFER FROM SAV') as `INTERNAL_TRANSFER_IGNORE`.
   - Tag peer-to-peer gifts or one-off help (e.g., 'ZELLE FROM MOM') as `ONE_OFF_GIFT_OR_TRANSFER_IGNORE`.
3. TRADELINE DEBT RECONCILIATION (PREVENT DOUBLE COUNTING):
   - Compare every recurring loan/credit payment outflow in the bank statement against the provided `Known Credit Bureau Tradelines`.
   - If a bank outflow corresponds to a known bureau tradeline (e.g., 'CHASE AUTO FIN' matches 'JPMCB - CHASE AUTO', or 'NAVIENT ED' matches 'NAVIENT STUDENT LOAN'), you MUST tag it as `MATCHED_BUREAU_DEBT` and set `matched_tradeline_id` to that tradeline's ID.
   - If a bank outflow is a recurring debt/loan/Buy-Now-Pay-Later (e.g., Affirm, Klarna, SpeedyCash Payday Loan) that does NOT appear in `Known Credit Bureau Tradelines`, tag it as `UNDISCLOSED_DEBT_PAYMENT`.
4. FEE CLASSIFICATION:
   - Tag punitive overdraft or Non-Sufficient Funds charges (e.g., 'RETURNED ITEM UNPAID NSF FEE', 'OVERDRAFT ITEM FEE') as `NSF_OVERDRAFT_FEE`.
   - Tag standard monthly checking account service fees (e.g., 'MONTHLY SERVICE MAINTENANCE FEE') as `BANK_MAINTENANCE_FEE_IGNORE`.
"""

        human_prompt = """Applicant Name: {applicant_name}

=== KNOWN CREDIT BUREAU TRADELINES ===
{tradelines_json}

=== PRE-FILTERED 90-DAY BANK TRANSACTIONS TO CLASSIFY ===
{transactions_json}

Classify every single transaction in the list above and provide a concise analyst_summary."""

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("human", human_prompt),
        ])

        # --- VARIANT 1: Standard Python Invocation (No LCEL Pipe) ---
        formatted_messages = prompt.format_messages(
            applicant_name=state["applicant_name"],
            tradelines_json=json.dumps(state["credit_report"]["tradelines"], indent=2),
            transactions_json=json.dumps(state["prefiltered_transactions"], indent=2),
        )
        result: BankStatementAnalysisOutput = structured_llm.invoke(formatted_messages)

        logs.append(
            f"🤖 [Node 3: Bank Statement Analyzer Agent] Categorized {len(result.categorized_transactions)} "
            f"transactions and reconciled against {len(state['credit_report']['tradelines'])} bureau tradelines."
        )

        return {
            # Convert Pydantic v2 objects to standard Python dicts for clean JSON serialization
            "categorized_transactions": [
                tx.model_dump() for tx in result.categorized_transactions
            ],
            "bank_analyst_summary": result.analyst_summary,
            "execution_log": logs,
        }

    except Exception as exc:
        err_msg = f"Bank Analyzer Agent Exception: {str(exc)}"
        logs.append(f"⚠️ [Node 3: Bank Statement Analyzer Agent] Failed -> {err_msg}")
        return {
            "error_message": err_msg,
            "execution_log": logs,
        }


# ---------------------------------------------------------------------------
# NODE 4: DETERMINISTIC PYTHON FINANCIAL RATIO CALCULATOR
# ---------------------------------------------------------------------------
def calculate_financial_ratios_node(state: LoanApplicationState) -> Dict[str, Any]:
    """
    Pure Python Math Node (SR 11-7 Compliant):
    Computes the core underwriting metrics from the Credit Bureau JSON and the
    LLM-categorized bank transactions with 100% mathematical precision.
    """
    logs = list(state.get("execution_log") or [])

    if state.get("error_message") or not state.get("categorized_transactions"):
        err = state.get("error_message") or "Missing categorized transactions for ratio calculation."
        logs.append(f"⚠️ [Node 4: Ratio Calculator] Skipping math due to upstream error: {err}")
        return {"execution_log": logs}

    try:
        credit_report = state["credit_report"]
        bank_raw = state["bank_statement_raw"]
        categorized_txs = state["categorized_transactions"]
        months = float(bank_raw.get("statement_period_months", 3))

        # 1. FICO Score
        fico_score = int(credit_report["fico_score"])

        # 2. Active Bankruptcy Check (Ignore DISCHARGED historical bankruptcies)
        has_active_bankruptcy = any(
            "BANKRUPTCY" in rec.get("type", "").upper()
            and rec.get("status", "").upper() == "ACTIVE"
            for rec in credit_report.get("public_records", [])
        )

        # 3. Total 12-Month Delinquencies Across All Tradelines
        total_delinquencies_12m = sum(
            int(tl["delinquencies_12m"].get("30_days", 0))
            + int(tl["delinquencies_12m"].get("60_days", 0))
            + int(tl["delinquencies_12m"].get("90_plus_days", 0))
            for tl in credit_report.get("tradelines", [])
        )

        # 4. Punitive NSF / Overdraft Event Count in 90 Days
        nsf_count_90d = sum(
            1 for tx in categorized_txs if tx["category"] == "NSF_OVERDRAFT_FEE"
        )

        # 5. Verified Monthly Income & Income Variance %
        total_verified_payroll = sum(
            float(tx["amount"])
            for tx in categorized_txs
            if tx["category"] == "VERIFIED_PAYROLL_INCOME" and float(tx["amount"]) > 0
        )
        verified_monthly_income = round(total_verified_payroll / months, 2)
        stated_monthly_income = round(float(state["stated_annual_income"]) / 12.0, 2)

        if stated_monthly_income > 0:
            income_variance_pct = round(
                abs(stated_monthly_income - verified_monthly_income)
                / stated_monthly_income
                * 100.0,
                2,
            )
        else:
            income_variance_pct = 100.0

        # 6. Estimated New Monthly Loan Payment (36 months, 12% APR -> r = 0.01)
        loan_amt = float(state["loan_amount_requested"])
        r = 0.12 / 12.0
        n = 36
        estimated_new_payment = round(
            loan_amt * (r * ((1 + r) ** n)) / (((1 + r) ** n) - 1), 2
        )

        # 7. Reconciled Post-Loan Debt-to-Income Ratio (DTI)
        bureau_monthly_debt = round(
            sum(float(tl["monthly_payment"]) for tl in credit_report.get("tradelines", [])),
            2,
        )
        total_undisclosed_debt_90d = sum(
            abs(float(tx["amount"]))
            for tx in categorized_txs
            if tx["category"] == "UNDISCLOSED_DEBT_PAYMENT"
        )
        undisclosed_monthly_debt = round(total_undisclosed_debt_90d / months, 2)

        total_post_loan_debt = (
            bureau_monthly_debt + undisclosed_monthly_debt + estimated_new_payment
        )
        if verified_monthly_income > 0:
            post_loan_dti = round(
                (total_post_loan_debt / verified_monthly_income) * 100.0, 2
            )
        else:
            post_loan_dti = 999.0

        # 8. Liquidity Cushion Ratio (Avg Ending Balance / New Monthly Payment)
        eom_balances = [
            float(b["ending_balance"]) for b in bank_raw.get("eom_balances", [])
        ]
        avg_ending_balance = (
            round(sum(eom_balances) / len(eom_balances), 2) if eom_balances else 0.0
        )
        payment_cushion_ratio = (
            round(avg_ending_balance / estimated_new_payment, 2)
            if estimated_new_payment > 0
            else 0.0
        )

        logs.append(
            f"🧮 [Node 4: Deterministic Ratio Calculator] Verified Income=${verified_monthly_income:,.2f}/mo | "
            f"Bureau Debt=${bureau_monthly_debt:,.2f} | Hidden Debt=${undisclosed_monthly_debt:,.2f} | "
            f"New Pmt=${estimated_new_payment:,.2f} | Post-Loan DTI={post_loan_dti}% | NSF={nsf_count_90d}"
        )

        return {
            "fico_score": fico_score,
            "has_active_bankruptcy": has_active_bankruptcy,
            "total_delinquencies_12m": total_delinquencies_12m,
            "nsf_count_90d": nsf_count_90d,
            "bureau_monthly_debt": bureau_monthly_debt,
            "undisclosed_monthly_debt": undisclosed_monthly_debt,
            "verified_monthly_income": verified_monthly_income,
            "stated_monthly_income": stated_monthly_income,
            "income_variance_pct": income_variance_pct,
            "estimated_new_Loan_payment": estimated_new_payment,
            "post_loan_dti": post_loan_dti,
            "avg_ending_balance": avg_ending_balance,
            "payment_cushion_ratio": payment_cushion_ratio,
            "execution_log": logs,
        }

    except Exception as exc:
        err_msg = f"Ratio Calculation Exception: {str(exc)}"
        logs.append(f"⚠️ [Node 4: Ratio Calculator] Failed -> {err_msg}")
        return {
            "error_message": err_msg,
            "execution_log": logs,
        }


# ---------------------------------------------------------------------------
# NODE 5: AGENT #2 - POLICY UNDERWRITER (Teaches LCEL Pipe + Zero Hardcoded Rules)
# ---------------------------------------------------------------------------
def underwriter_agent_node(state: LoanApplicationState) -> Dict[str, Any]:
    """
    Evaluates the calculated financial metrics dynamically against policy_v1.md.
    - ZERO hardcoded rule numbers or numeric thresholds in the prompt!
    - Teaches Variant 2: LCEL Pipe (`chain = prompt | structured_llm`).
    - Fails safe to ESCALATE (human_review_node) if any upstream or LLM error occurs.
    """
    logs = list(state.get("execution_log") or [])

    # Fail-Safe Check: If an upstream step recorded an error, route straight to ESCALATE for Human Review
    if state.get("error_message"):
        err = state["error_message"]
        logs.append(
            f"🚨 [Node 5: Policy Underwriter Agent] Upstream error detected. Failing safe to ESCALATE (Human Review)."
        )
        return {
            "underwriting_decision": "ESCALATE",
            "confidence_score": 0.0,
            "policy_citations": ["SYSTEM_EXCEPTION_FALLBACK"],
            "adverse_action_reasons": [],
            "underwriter_memo": (
                f"AUTOMATED PIPELINE EXCEPTION — MANUAL UNDERWRITING REQUIRED.\n"
                f"Reason: {err}\nPlease review raw applicant files manually."
            ),
            "execution_log": logs,
        }

    try:
        llm = get_llm()
        structured_llm = llm.with_structured_output(UnderwritingOutput)
        policy_markdown = load_policy_document()

        # Notice: ZERO hardcoded rule IDs or numeric thresholds in this prompt!
        system_prompt = """You are the Lead Credit Underwriter AI Agent.
Your task is to evaluate the applicant's audited financial metrics strictly against the provided Official Underwriting Policy document.

=== OFFICIAL UNDERWRITING POLICY ===
{policy_markdown}

CRITICAL OPERATING PRINCIPLES:
1. Single Source of Truth: Derive all thresholds, knockout criteria, approval criteria, escalation triggers, and rule citations exclusively from the Official Underwriting Policy text above.
2. Mathematical Integrity: Do not recalculate any numbers. Trust the exact pre-computed metrics provided in the applicant summary.
3. Evaluation Hierarchy:
   - First, check if the applicant triggers any automatic denial / knockout rules in the policy. If so, output `DENY`, cite all violated rule identifiers from the policy text, and provide clear consumer-facing `adverse_action_reasons`.
   - Second, if no knockout rules are violated, check if the applicant satisfies every requirement for standard automatic approval in the policy. If all are satisfied, output `APPROVE` and cite the corresponding approval rule identifiers.
   - Third, if the applicant neither violates a knockout rule nor satisfies all automatic approval rules, output `ESCALATE`, cite the specific manual escalation rule identifiers triggered from the policy text, and write a comprehensive `underwriter_memo` detailing compensating factors and required human verification steps.
"""

        verified_annual_income = round(state["verified_monthly_income"] * 12.0, 2)
        loan_to_verified_income_pct = (
            round((state["loan_amount_requested"] / verified_annual_income) * 100.0, 2)
            if verified_annual_income > 0
            else 999.0
        )

        # Notice: Pure factual data presentation—no policy hints in the human prompt!
        human_prompt = """=== APPLICANT & LOAN REQUEST ===
- Application ID: {application_id}
- Applicant Name: {applicant_name}
- Requested Loan Amount: ${loan_amount_requested:,.2f} (Purpose: {loan_purpose})
- Stated Annual Income: ${stated_annual_income:,.2f} (${stated_monthly_income:,.2f}/mo)

=== FCRA CONSENT ===
- Is FCRA Consent provided: {fcra_consent}

=== AUDITED FINANCIAL METRICS & RATIOS ===
- FICO Score: {fico_score}
- Active Bankruptcy on File: {has_active_bankruptcy}
- Total Delinquencies (Last 12 Months): {total_delinquencies_12m}
- Punitive NSF / Overdraft Fee Events (Last 90 Days): {nsf_count_90d}
- Verified Net Monthly Payroll Income: ${verified_monthly_income:,.2f}/mo (${verified_annual_income:,.2f}/yr)
- Requested Loan Amount as % of Verified Annualized Income: {loan_to_verified_income_pct}%
- Stated vs. Verified Monthly Income Variance: {income_variance_pct}%
- Monthly Debt Obligations Breakdown:
  * Credit Bureau Monthly Debt: ${bureau_monthly_debt:,.2f}/mo
  * Undisclosed Monthly Bank Debt: ${undisclosed_monthly_debt:,.2f}/mo
  * Estimated New Loan Monthly Payment: ${estimated_new_Loan_payment:,.2f}/mo
- Reconciled Post-Loan Debt-to-Income (DTI) Ratio: {post_loan_dti}%
- Average Ending Monthly Bank Balance: ${avg_ending_balance:,.2f}
- Liquidity Payment Cushion Ratio (Avg Ending Balance / New Loan Payment): {payment_cushion_ratio}x

=== BANK STATEMENT ANALYST SUMMARY ===
{bank_analyst_summary}

Evaluate these facts against the policy document and return your structured decision."""

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("human", human_prompt),
        ])

        # --- VARIANT 2: LCEL Pipe Invocation (`prompt | structured_llm`) ---
        chain = prompt | structured_llm
        decision_output: UnderwritingOutput = chain.invoke({
            "policy_markdown": policy_markdown,
            "application_id": state["application_id"],
            "applicant_name": state["applicant_name"],
            "loan_amount_requested": state["loan_amount_requested"],
            "loan_purpose": state["loan_purpose"],
            "stated_annual_income": state["stated_annual_income"],
            "stated_monthly_income": state["stated_monthly_income"],
            "fcra_consent": state["fcra_consent"],
            "fico_score": state["fico_score"],
            "has_active_bankruptcy": state["has_active_bankruptcy"],
            "total_delinquencies_12m": state["total_delinquencies_12m"],
            "nsf_count_90d": state["nsf_count_90d"],
            "verified_monthly_income": state["verified_monthly_income"],
            "verified_annual_income": verified_annual_income,
            "loan_to_verified_income_pct": loan_to_verified_income_pct,
            "income_variance_pct": state["income_variance_pct"],
            "bureau_monthly_debt": state["bureau_monthly_debt"],
            "undisclosed_monthly_debt": state["undisclosed_monthly_debt"],
            "estimated_new_Loan_payment": state["estimated_new_Loan_payment"],
            "post_loan_dti": state["post_loan_dti"],
            "avg_ending_balance": state["avg_ending_balance"],
            "payment_cushion_ratio": state["payment_cushion_ratio"],
            "bank_analyst_summary": state["bank_analyst_summary"],
        })

        logs.append(
            f"⚖️ [Node 5: Policy Underwriter Agent] Decision={decision_output.underwriting_decision} "
            f"| Citations={', '.join(decision_output.policy_citations)}"
        )

        return {
            "underwriting_decision": decision_output.underwriting_decision,
            "confidence_score": decision_output.confidence_score,
            "policy_citations": decision_output.policy_citations,
            "adverse_action_reasons": decision_output.adverse_action_reasons,
            "underwriter_memo": decision_output.underwriter_memo,
            "execution_log": logs,
        }

    except Exception as exc:
        err_msg = f"Underwriter Agent Exception: {str(exc)}"
        logs.append(
            f"🚨 [Node 5: Policy Underwriter Agent] Exception caught -> Failing safe to ESCALATE: {err_msg}"
        )
        return {
            "error_message": err_msg,
            "underwriting_decision": "ESCALATE",
            "confidence_score": 0.0,
            "policy_citations": ["SYSTEM_EXCEPTION_FALLBACK"],
            "adverse_action_reasons": [],
            "underwriter_memo": (
                f"AUTOMATED UNDERWRITER EXCEPTION — ROUTED TO HUMAN REVIEW.\n"
                f"Details: {err_msg}"
            ),
            "execution_log": logs,
        }


def route_after_underwriter(
    state: LoanApplicationState,
) -> Literal["NEEDS_HUMAN_REVIEW", "READY_TO_FINALIZE"]:
    """
    Routing Style #2 (Symbolic Intent Strings mapped via Dict):
    Returns a logical routing label rather than a hardcoded node name.
    `add_conditional_edges` maps these labels to the actual target nodes.
    """
    if state.get("underwriting_decision") == "ESCALATE":
        return "NEEDS_HUMAN_REVIEW"
    return "READY_TO_FINALIZE"


# ---------------------------------------------------------------------------
# NODE 6A & 6B: HUMAN-IN-THE-LOOP ESCALATION & FINALIZATION
# ---------------------------------------------------------------------------
def human_review_node(state: LoanApplicationState) -> Dict[str, Any]:
    """
    Executes AFTER the graph is resumed by the Human Underwriter in the UI.
    Reads `human_reviewer_decision` and `human_reviewer_notes` injected via update_state().
    """
    logs = list(state.get("execution_log") or [])
    human_decision = state.get("human_reviewer_decision", "DENY")
    human_notes = state.get("human_reviewer_notes", "Reviewed by human underwriter.")
    logs.append(
        f"👤 [Node 6A: Human Underwriter Override] Final Decision={human_decision} | Notes: {human_notes}"
    )
    return {
        "final_status": f"HUMAN_{human_decision}D",
        "execution_log": logs,
    }


def finalize_application_node(state: LoanApplicationState) -> Dict[str, Any]:
    """Sets final_status for automated approvals, denials, or human-reviewed loans."""
    logs = list(state.get("execution_log") or [])
    if not state.get("final_status"):
        decision = state.get("underwriting_decision", "DENY")
        if decision == "APPROVE":
            final_status = "AUTO_APPROVED"
        elif decision == "DENY":
            final_status = "AUTO_DENIED"
        else:
            final_status = decision
    else:
        final_status = state["final_status"]

    logs.append(
        f"🏁 [Node 6B: Finalize Application] Workflow complete -> Status: {final_status}"
    )
    return {"final_status": final_status, "execution_log": logs}


# ---------------------------------------------------------------------------
# BUILD & COMPILE THE LANGGRAPH WORKFLOW
# ---------------------------------------------------------------------------
def build_underwriting_graph(checkpointer=None):
    """
    Compiles the 6-node StateGraph with:
    - RetryPolicy on LLM nodes for transient rate-limit resilience
    - `interrupt_before=["human_review_node"]` for Human-in-the-Loop escalation
    """
    if checkpointer is None:
        checkpointer = MemorySaver()

    builder = StateGraph(LoanApplicationState)

    # Standard retry policy for external/LLM nodes (handles transient 429 rate limits)
    llm_retry_policy = RetryPolicy(max_attempts=3, initial_interval=1.5, backoff_factor=2.0)

    # Register all nodes
    builder.add_node("validate_consent_node", validate_consent_node)
    builder.add_node("pull_credit_bureau_node", pull_credit_bureau_node)
    builder.add_node(
        "pull_raw_bank_transactions_node", pull_raw_bank_transactions_node
    )
    builder.add_node(
        "bank_statement_analyzer_agent",
        bank_statement_analyzer_agent,
        retry_policy=llm_retry_policy,
    )
    builder.add_node(
        "calculate_financial_ratios_node", calculate_financial_ratios_node
    )
    builder.add_node(
        "underwriter_agent_node",
        underwriter_agent_node,
        retry_policy=llm_retry_policy,
    )
    builder.add_node("human_review_node", human_review_node)
    builder.add_node("finalize_application_node", finalize_application_node)

    # 1. Entry -> Consent Gatekeeper
    builder.add_edge(START, "validate_consent_node")

    # 2. Conditional Edge Variant A (List of valid target node names, supporting Parallel Fan-Out)
    builder.add_conditional_edges(
        "validate_consent_node",
        route_after_consent,
        [
            "pull_credit_bureau_node",
            "pull_raw_bank_transactions_node",
            "finalize_application_node",
        ],
    )

    # 3. Parallel Fan-In Barrier: Wait for BOTH 2A and 2B to complete before running Node 3
    builder.add_edge(
        ["pull_credit_bureau_node", "pull_raw_bank_transactions_node"],
        "bank_statement_analyzer_agent",
    )

    # 4. Sequential Edges: Analyzer -> Python Ratio Math -> Policy Underwriter
    builder.add_edge(
        "bank_statement_analyzer_agent", "calculate_financial_ratios_node"
    )
    builder.add_edge("calculate_financial_ratios_node", "underwriter_agent_node")

    # 5. Conditional Edge Variant B (Dict mapping symbolic intent strings -> target node names)
    builder.add_conditional_edges(
        "underwriter_agent_node",
        route_after_underwriter,
        {
            "NEEDS_HUMAN_REVIEW": "human_review_node",
            "READY_TO_FINALIZE": "finalize_application_node",
        },
    )

    # 6. Human Review -> Finalize -> END
    builder.add_edge("human_review_node", "finalize_application_node")
    builder.add_edge("finalize_application_node", END)

    # Compile with Human-in-the-Loop breakpoint before human_review_node
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_review_node"],
    )
