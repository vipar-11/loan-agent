import json
import os
import re
from typing import Dict, Any, List, Generator
from unittest.mock import MagicMock
import pytest
from langchain_core.runnables import RunnableLambda

from loan_agent.db import ApplicationDatabase, get_application_database
from loan_agent.checkpointer import get_langgraph_checkpointer
from loan_agent.schema import BankStatementAnalysisOutput, CategorizedTransaction, UnderwritingOutput


def _mock_bank_statement_analyzer(input_val):
    if isinstance(input_val, list):
        text = input_val[-1].content
    elif hasattr(input_val, "to_string"):
        text = input_val.to_string()
    else:
        text = str(input_val)

    txs = []
    tls = []
    try:
        if "=== PRE-FILTERED 90-DAY BANK TRANSACTIONS TO CLASSIFY ===" in text:
            tx_part = text.split("=== PRE-FILTERED 90-DAY BANK TRANSACTIONS TO CLASSIFY ===")[1]
            if "Classify every single transaction" in tx_part:
                tx_part = tx_part.split("Classify every single transaction")[0]
            txs = json.loads(tx_part.strip())
        else:
            all_json = re.findall(r"\[\s*\{.*?\}\s*\]", text, re.DOTALL)
            txs = json.loads(all_json[-1]) if all_json else []

        if "=== KNOWN CREDIT BUREAU TRADELINES ===" in text:
            tl_part = text.split("=== KNOWN CREDIT BUREAU TRADELINES ===")[1].split("===")[0]
            tls = json.loads(tl_part.strip())
    except Exception:
        pass

    categorized = []
    for i, t in enumerate(txs):
        desc = (t.get("description") or "").upper()
        amt = float(t.get("amount") or 0.0)
        matched_id = None

        if any(kw in desc for kw in ["PAYROLL", "DIRECT DEP", "SALARY", "GUSTO", "ADP", "EMPLOYER"]) or (amt > 1500 and "TRANSFER" not in desc):
            cat = "VERIFIED_PAYROLL_INCOME"
        elif any(kw in desc for kw in ["NSF", "OVERDRAFT", "RETURNED ITEM"]):
            cat = "NSF_OVERDRAFT_FEE"
        elif any(kw in desc for kw in ["KLARNA", "AFFIRM", "AFTERPAY", "PAYDAY", "SPEEDYCASH"]):
            cat = "UNDISCLOSED_DEBT_PAYMENT"
        elif any(kw in desc for kw in ["MONTHLY SERVICE", "MAINTENANCE FEE"]):
            cat = "BANK_MAINTENANCE_FEE_IGNORE"
        elif any(kw in desc for kw in ["XFER", "TRANSFER"]):
            cat = "INTERNAL_TRANSFER_IGNORE"
        elif any(kw in desc for kw in ["ZELLE", "VENMO"]):
            cat = "ONE_OFF_GIFT_OR_TRANSFER_IGNORE"
        else:
            # Check tradelines
            matched = False
            for tl in tls:
                cred = (tl.get("creditor") or tl.get("creditor_name") or "").upper()
                if cred and (cred in desc or desc in cred or any(w in desc for w in cred.split() if len(w) > 3)):
                    matched = True
                    matched_id = tl.get("tradeline_id")
                    break
            if matched:
                cat = "MATCHED_BUREAU_DEBT"
            elif any(kw in desc for kw in ["GROCERY", "RESTAURANT", "GAS", "EXPENSE", "STORE"]) or amt < 0:
                cat = "OTHER_EXPENSE_IGNORE"
            else:
                cat = "ONE_OFF_GIFT_OR_TRANSFER_IGNORE"

        categorized.append(CategorizedTransaction(
            tx_id=t.get("tx_id") or f"TX_{i+1}",
            date=t.get("date", "2026-07-01"),
            description=t.get("description", ""),
            amount=amt,
            category=cat,
            matched_tradeline_id=matched_id,
            reasoning=f"Classified as {cat}",
        ))

    return BankStatementAnalysisOutput(
        categorized_transactions=categorized,
        analyst_summary=f"Categorized {len(categorized)} transactions.",
    )


def _mock_underwriting_decision(input_val):
    if hasattr(input_val, "to_string"):
        text = input_val.to_string()
    elif isinstance(input_val, list):
        text = input_val[-1].content
    else:
        text = str(input_val)

    metrics_text = text
    if "=== AUDITED FINANCIAL METRICS & RATIOS ===" in text:
        metrics_text = text.split("=== AUDITED FINANCIAL METRICS & RATIOS ===")[1]
        if "===" in metrics_text:
            metrics_text = metrics_text.split("===")[0]

    fico_m = re.search(r"FICO Score:\s*(\d+)", metrics_text)
    fico = int(fico_m.group(1)) if fico_m else 700
    bk = "Active Bankruptcy on File: True" in metrics_text
    delinq_m = re.search(r"Total Delinquencies[^:]*:\s*(\d+)", metrics_text)
    delinq = int(delinq_m.group(1)) if delinq_m else 0
    nsf_m = re.search(r"Punitive NSF[^:]*:\s*(\d+)", metrics_text)
    nsf = int(nsf_m.group(1)) if nsf_m else 0
    dti_m = re.search(r"Reconciled Post-Loan Debt-to-Income \(DTI\) Ratio:\s*([\d.]+)", metrics_text)
    dti = float(dti_m.group(1)) if dti_m else 25.0
    var_m = re.search(r"Income Variance:\s*([\d.]+)%", metrics_text)
    var_pct = float(var_m.group(1)) if var_m else 0.0
    loan_m = re.search(r"Requested Loan Amount:\s*\$([0-9,.]+)", text)
    loan_amt = float(loan_m.group(1).replace(",", "")) if loan_m else 0.0

    if fico < 620 or bk or delinq > 1 or nsf > 2 or dti > 45.0:
        decision = "DENY"
        confidence = 0.95
        citations = ["POLICY_CREDIT_CRITERIA", "POLICY_KNOCKOUT_RULE"]
        adverse = ["Credit score, delinquency, or debt-to-income threshold not met."]
        memo = "Automated denial per risk standards."
    elif loan_amt > 25000 or var_pct > 15.0 or dti > 36.0:
        decision = "ESCALATE"
        confidence = 0.85
        citations = ["POLICY_MANUAL_REVIEW"]
        adverse = []
        memo = "Escalated for human credit committee review."
    else:
        decision = "APPROVE"
        confidence = 0.95
        citations = ["POLICY_APPROVAL_STANDARD"]
        adverse = []
        memo = "Automated approval per policy."

    return UnderwritingOutput(
        underwriting_decision=decision,
        confidence_score=confidence,
        policy_citations=citations,
        adverse_action_reasons=adverse,
        underwriter_memo=memo,
    )


class TestMockLLM:
    def with_structured_output(self, schema):
        if schema == BankStatementAnalysisOutput:
            return RunnableLambda(_mock_bank_statement_analyzer)
        elif schema == UnderwritingOutput:
            return RunnableLambda(_mock_underwriting_decision)
        return MagicMock()


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch, tmp_path) -> Generator[None, None, None]:
    """
    Guarantees that test cases run completely isolated from real local or remote databases,
    using in-memory SQLite (:memory:) by default, and without exercising live LLMs unless explicitly specified.
    Any partner batch-intake monitor started by a test works on a per-test temporary folder, never data/intake.
    """
    monkeypatch.setenv("BATCH_INTAKE_ROOT", str(tmp_path / "intake"))
    import loan_agent.db as db_mod
    import loan_agent.checkpointer as cp_mod

    # Pristine in-memory DB instances per test
    default_test_app_db = ApplicationDatabase(in_memory=True)
    default_test_checkpointer = get_langgraph_checkpointer(in_memory=True)

    orig_get_app_db = db_mod.get_application_database
    orig_get_checkpointer = cp_mod.get_langgraph_checkpointer

    def _test_safe_get_app_db(config=None, in_memory=False, force_new=False):
        if in_memory:
            return ApplicationDatabase(config=config, in_memory=True)
        # If an explicit config was passed pointing to a custom path (e.g. tmp_path) or remote mock
        if config is not None and (
            config.mode == "remote"
            or (config.local_app_db_path and "data/loan_agent_app.db" not in str(config.local_app_db_path))
        ):
            return orig_get_app_db(config=config, in_memory=False, force_new=force_new)
        return default_test_app_db

    def _test_safe_get_checkpointer(config=None, in_memory=False, force_new=False):
        if in_memory:
            return get_langgraph_checkpointer(config=config, in_memory=True, force_new=force_new)
        # If an explicit config was passed pointing to a custom path (e.g. tmp_path) or remote mock
        if config is not None and (
            config.mode == "remote"
            or (config.local_langgraph_db_path and "data/loan_agent_checkpoints.db" not in str(config.local_langgraph_db_path))
        ):
            return orig_get_checkpointer(config=config, in_memory=False, force_new=force_new)
        return default_test_checkpointer

    monkeypatch.setattr("loan_agent.db.get_application_database", _test_safe_get_app_db)
    monkeypatch.setattr("loan_agent.checkpointer.get_langgraph_checkpointer", _test_safe_get_checkpointer)
    monkeypatch.setattr("loan_agent.workflow_runner.get_application_database", _test_safe_get_app_db)
    monkeypatch.setattr("loan_agent.workflow_runner.get_langgraph_checkpointer", _test_safe_get_checkpointer)
    monkeypatch.setattr("loan_agent.admin.get_application_database", _test_safe_get_app_db)
    monkeypatch.setattr("loan_agent.admin.get_langgraph_checkpointer", _test_safe_get_checkpointer)

    # Enforce test mock LLM unless explicitly overridden with RUN_LIVE_LLM=1
    if os.environ.get("RUN_LIVE_LLM") != "1":
        monkeypatch.setattr("loan_agent.graph_workflow.get_llm", lambda *a, **kw: TestMockLLM())

    yield

    default_test_app_db.close()
    if hasattr(default_test_checkpointer, "conn") and default_test_checkpointer.conn:
        try:
            default_test_checkpointer.conn.close()
        except Exception:
            pass


@pytest.fixture
def test_app_db() -> Generator[ApplicationDatabase, None, None]:
    """Provides a pristine, isolated in-memory ApplicationDatabase."""
    db = ApplicationDatabase(in_memory=True)
    yield db
    db.close()


@pytest.fixture
def test_checkpointer() -> Generator[Any, None, None]:
    """Provides an isolated in-memory LangGraph checkpointer."""
    cp = get_langgraph_checkpointer(in_memory=True)
    yield cp
    if hasattr(cp, "conn") and cp.conn:
        try:
            cp.conn.close()
        except Exception:
            pass



@pytest.fixture
def clean_credit_report() -> Dict[str, Any]:
    """Experian credit report with prime score, 0 delinquencies, 0 active bankruptcies."""
    return {
        "bureau": "Experian",
        "fico_score": 750,
        "public_records": [
            {"type": "CHAPTER_7_BANKRUPTCY", "status": "DISCHARGED", "filed_date": "2018-05-10"}
        ],
        "tradelines": [
            {
                "tradeline_id": "TL_001",
                "creditor": "HONDA FINANCIAL",
                "account_type": "AUTO_LOAN",
                "monthly_payment": 450.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0},
            },
            {
                "tradeline_id": "TL_002",
                "creditor": "WELLS FARGO MORTGAGE",
                "account_type": "MORTGAGE",
                "monthly_payment": 950.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0},
            },
        ],
    }


@pytest.fixture
def subprime_credit_report() -> Dict[str, Any]:
    """Experian credit report with subprime score, active bankruptcy, and multiple delinquencies."""
    return {
        "bureau": "Experian",
        "fico_score": 580,
        "public_records": [
            {"type": "CHAPTER_13_BANKRUPTCY", "status": "ACTIVE", "filed_date": "2025-01-15"}
        ],
        "tradelines": [
            {
                "tradeline_id": "TL_101",
                "creditor": "CAPITAL ONE",
                "account_type": "CREDIT_CARD",
                "monthly_payment": 250.00,
                "delinquencies_12m": {"30_days": 2, "60_days": 1, "90_plus_days": 0},
            },
            {
                "tradeline_id": "TL_102",
                "creditor": "SANTANDER CONSUMER",
                "account_type": "AUTO_LOAN",
                "monthly_payment": 550.00,
                "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 1},
            },
        ],
    }


@pytest.fixture
def clean_bank_statement() -> Dict[str, Any]:
    """Plaid 90-day bank ledger with consistent payroll deposits and strong liquidity."""
    return {
        "bank_name": "Chase Checking",
        "account_mask": "0000",
        "statement_period_months": 3,
        "eom_balances": [
            {"month": "2026-07", "ending_balance": 8500.00},
            {"month": "2026-08", "ending_balance": 9200.00},
            {"month": "2026-09", "ending_balance": 9000.00},
        ],
        "transactions": [
            {"tx_id": "T01", "date": "2026-07-01", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
            {"tx_id": "T02", "date": "2026-07-05", "description": "HONDA FINANCIAL ACH", "amount": -450.00},
            {"tx_id": "T03", "date": "2026-07-15", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
            {"tx_id": "T04", "date": "2026-08-01", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
            {"tx_id": "T05", "date": "2026-08-15", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
            {"tx_id": "T06", "date": "2026-09-01", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
            {"tx_id": "T07", "date": "2026-09-15", "description": "CORP PAYROLL DIRECT DEP", "amount": 4000.00},
        ],
    }


@pytest.fixture
def categorized_transactions_clean() -> List[Dict[str, Any]]:
    """Sample transactions categorized into verified income and debt."""
    return [
        {"tx_id": "T01", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
        {"tx_id": "T02", "category": "MATCHED_BUREAU_DEBT", "amount": -450.00},
        {"tx_id": "T03", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
        {"tx_id": "T04", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
        {"tx_id": "T05", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
        {"tx_id": "T06", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
        {"tx_id": "T07", "category": "VERIFIED_PAYROLL_INCOME", "amount": 4000.00},
    ]


@pytest.fixture
def categorized_transactions_distressed() -> List[Dict[str, Any]]:
    """Sample transactions with NSF fees and undisclosed BNPL debt."""
    return [
        {"tx_id": "M01", "category": "VERIFIED_PAYROLL_INCOME", "amount": 2500.00},
        {"tx_id": "M02", "category": "VERIFIED_PAYROLL_INCOME", "amount": 2500.00},
        {"tx_id": "M03", "category": "NSF_OVERDRAFT_FEE", "amount": -35.00},
        {"tx_id": "M04", "category": "NSF_OVERDRAFT_FEE", "amount": -35.00},
        {"tx_id": "M05", "category": "NSF_OVERDRAFT_FEE", "amount": -35.00},
        {"tx_id": "M06", "category": "UNDISCLOSED_DEBT_PAYMENT", "amount": -450.00},
        {"tx_id": "M07", "category": "ONE_OFF_GIFT_OR_TRANSFER_IGNORE", "amount": 500.00},
    ]
