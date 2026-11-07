"""
Workflow execution engine for loan applications.

Provides the reusable, UI-agnostic entry points shared by the CLI scheduler
(`python -m initiate_workflow`) and the FastAPI service layer:

- `process_application()`       Runs the agentic underwriting workflow for ONE application and persists the outcome.
- `run_initiate_workflow()`     Batch-runs the pipeline for every NEW application (FIFO, optional limit / dry-run).
- `resume_with_human_decision()` Injects a credit officer's determination into the paused graph and resumes it.

All graph state is checkpointed in a distinct database from the application records.
"""
from datetime import datetime
from typing import Dict, Any, Optional, List, Literal
import argparse
import sys
from dotenv import load_dotenv

load_dotenv()

from loan_agent.config import DatabaseConfig, load_db_config
from loan_agent.db import ApplicationDatabase, get_application_database
from loan_agent.checkpointer import get_langgraph_checkpointer
from loan_agent.graph_workflow import build_underwriting_graph, human_final_status
from loan_agent.state import LoanApplicationState


def normalize_cli_args(args: List[str]) -> List[str]:
    """Normalizes Unicode em-dashes, en-dashes, and curly quotes in CLI argument flags."""
    normalized = []
    for arg in args:
        cleaned = arg
        if cleaned.startswith("—") or cleaned.startswith("–"):
            cleaned = "--" + cleaned[1:]
        cleaned = cleaned.replace("“", "").replace("”", "").replace('"', "")
        normalized.append(cleaned)
    return normalized


def build_initial_state(loan: Dict[str, Any]) -> LoanApplicationState:
    """Projects a persisted application record onto the workflow input state."""
    return {
        "application_id": loan["application_id"],
        "applicant_name": loan["applicant_name"],
        "ssn_last_4": str(loan["ssn_last_4"]),
        "stated_annual_income": float(loan["stated_annual_income"]),
        "loan_amount_requested": float(loan["loan_amount_requested"]),
        "loan_purpose": loan["loan_purpose"],
        "fcra_consent": bool(loan["fcra_consent"]),
    }


def thread_config_for(application_id: str) -> Dict[str, Any]:
    """Checkpoint thread configuration: one workflow thread per loan application."""
    return {"configurable": {"thread_id": application_id}}


def process_application(
    loan: Dict[str, Any],
    graph: Optional[Any] = None,
    app_db: Optional[ApplicationDatabase] = None,
    checkpointer: Optional[Any] = None,
    config: Optional[DatabaseConfig] = None,
    initiator: str = "WORKFLOW_ENGINE",
) -> Dict[str, Any]:
    """
    Executes the underwriting pipeline for a single application and persists the outcome.

    Status transitions:
      NEW -> PENDING_PROCESSING -> AUTO_APPROVED | AUTO_DENIED | REJECTED_NO_CONSENT | PENDING_REVIEW (ESCALATE)
    Exceptions never propagate: the application fails safe into PENDING_REVIEW with the error recorded.

    Returns a summary item: {application_id, applicant_name, status, decision?, error?}.
    """
    db = app_db or get_application_database(config)
    if graph is None:
        graph = build_underwriting_graph(checkpointer=checkpointer or get_langgraph_checkpointer(config))

    app_id = loan["application_id"]
    name = loan["applicant_name"]

    # Mark as in-flight so queue consumers (UI / CLI) never double-dispatch the same file.
    db.update_application(
        application_id=app_id,
        updates={"status": "PENDING_PROCESSING"},
        actor=initiator,
        event_type="WORKFLOW_INITIATED",
        details="Approval workflow initiated; agentic workflow execution in progress.",
    )

    initial_state = build_initial_state(loan)
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        graph_output = graph.invoke(initial_state, config=thread_config_for(app_id))
        decision = graph_output.get("underwriting_decision", "ESCALATE")
        final_status = graph_output.get("final_status")

        if decision == "APPROVE":
            new_status = final_status or "AUTO_APPROVED"
            adjudication_info = {
                "decision": new_status,
                "officer": "AI Agent (Policy Underwriter)",
                "reason": graph_output.get("underwriter_memo", "Automated Approval"),
                "adjudicated_at": timestamp_str,
                "policy_citations": graph_output.get("policy_citations", []),
                "confidence_score": graph_output.get("confidence_score", 1.0),
            }
            event_type = "WORKFLOW_APPROVED"
            audit_details = f"Workflow completed with decision APPROVE ({new_status})"

        elif decision in ["DENY", "REJECT_NO_CONSENT"]:
            new_status = final_status or ("AUTO_DENIED" if decision == "DENY" else "REJECTED_NO_CONSENT")
            adjudication_info = {
                "decision": new_status,
                "officer": "AI Agent (Policy Underwriter)",
                "reason": graph_output.get("underwriter_memo", "Automated Denial"),
                "adjudicated_at": timestamp_str,
                "policy_citations": graph_output.get("policy_citations", []),
                "adverse_action_reasons": graph_output.get("adverse_action_reasons", []),
                "confidence_score": graph_output.get("confidence_score", 1.0),
            }
            event_type = "WORKFLOW_DENIED"
            audit_details = f"Workflow completed with decision DENY ({new_status})"

        else:  # ESCALATE -> graph is paused at the human_review_node breakpoint
            new_status = "PENDING_REVIEW"
            adjudication_info = None
            event_type = "WORKFLOW_ESCALATED"
            audit_details = "Workflow halted before human review node; escalated for credit officer review."

        db.update_application(
            application_id=app_id,
            updates={
                "status": new_status,
                "graph_state": graph_output,
                "adjudication": adjudication_info,
            },
            actor="WORKFLOW_ENGINE",
            event_type=event_type,
            details=audit_details,
        )
        return {
            "application_id": app_id,
            "applicant_name": name,
            "status": new_status,
            "decision": decision,
        }

    except Exception as exc:
        err_msg = f"Workflow execution error: {str(exc)}"
        fallback_state = {
            **initial_state,
            "underwriting_decision": "ESCALATE",
            "confidence_score": 0.0,
            "error_message": err_msg,
            "underwriter_memo": f"Execution halted due to exception: {err_msg}",
            "execution_log": [f"🚨 Exception: {err_msg}"],
        }
        db.update_application(
            application_id=app_id,
            updates={"status": "PENDING_REVIEW", "graph_state": fallback_state},
            actor="WORKFLOW_ENGINE",
            event_type="WORKFLOW_EXCEPTION",
            details=err_msg,
        )
        return {
            "application_id": app_id,
            "applicant_name": name,
            "status": "PENDING_REVIEW",
            "decision": "ESCALATE",
            "error": str(exc),
        }


def resume_with_human_decision(
    application_id: str,
    decision: Literal["APPROVE", "DENY"],
    notes: str,
    officer: str = "James Thornton, Senior Underwriter",
    app_db: Optional[ApplicationDatabase] = None,
    checkpointer: Optional[Any] = None,
    config: Optional[DatabaseConfig] = None,
) -> Dict[str, Any]:
    """
    Human-in-the-loop resumption: injects the credit officer's determination into the paused graph
    (`update_state`) and resumes execution (`invoke(None)`) through human_review_node -> finalize.

    Falls back to a synthesized final state if the checkpoint cannot be resumed, so the officer's
    determination is never lost. Returns the updated, persisted application record.

    Raises:
        KeyError:   application not found.
        ValueError: application not awaiting human review, or notes missing.
    """
    db = app_db or get_application_database(config)
    app = db.get_application(application_id)
    if not app:
        raise KeyError(f"Application '{application_id}' not found.")
    if app.get("status") != "PENDING_REVIEW":
        raise ValueError(f"Application '{application_id}' is not awaiting human review (status={app.get('status')}).")

    notes_clean = (notes or "").strip()
    if not notes_clean:
        raise ValueError("Mandatory: enter the underwriter justification memorandum before adjudicating.")

    officer_clean = (officer or "").strip() or "James Thornton, Senior Underwriter"
    graph_state = app.get("graph_state") or {}
    app_id = app["application_id"]
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        cp = checkpointer or get_langgraph_checkpointer(config)
        graph = build_underwriting_graph(checkpointer=cp)
        thread_config = thread_config_for(app_id)
        graph.update_state(
            thread_config,
            {"human_reviewer_decision": decision, "human_reviewer_notes": notes_clean},
        )
        final_result = graph.invoke(None, config=thread_config)
        final_status = final_result.get("final_status") or human_final_status(decision)
    except Exception as exc:
        final_status = human_final_status(decision)
        final_result = {
            **graph_state,
            "final_status": final_status,
            "human_reviewer_decision": decision,
            "human_reviewer_notes": notes_clean,
            "execution_log": list(graph_state.get("execution_log") or [])
            + [f"👤 Human Override: {decision} | Error resuming: {str(exc)}"],
        }

    adjudication = {
        "decision": final_status,
        "officer": officer_clean,
        "reason": notes_clean,
        "adjudicated_at": timestamp_str,
        "agent_notes": graph_state.get("underwriter_memo"),
        "policy_citations": graph_state.get("policy_citations"),
    }
    db.update_application(
        application_id=app_id,
        updates={"status": final_status, "graph_state": final_result, "adjudication": adjudication},
        actor=officer_clean,
        event_type="HUMAN_REVIEW_DECISION",
        details=f"Human determination recorded: {final_status}. Reason: {notes_clean}",
    )
    return db.get_application(app_id)  # type: ignore[return-value]


def run_initiate_workflow(
    dry_run: bool = False,
    limit: Optional[int] = None,
    config: Optional[DatabaseConfig] = None,
    app_db: Optional[ApplicationDatabase] = None,
    checkpointer: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Finds newly submitted loans and kick-starts the graph workflow for each.
    Supports dry-run preview and batch limit constraints.
    """
    cfg = config or load_db_config()
    db = app_db or get_application_database(cfg)
    cp = checkpointer or get_langgraph_checkpointer(cfg)

    # Fetch new loans (oldest first for FIFO processing)
    new_loans = db.list_applications(status="new")
    new_loans.sort(key=lambda x: x.get("submitted_at") or "")

    total_found = len(new_loans)
    if limit is not None and limit > 0:
        loans_to_process = new_loans[:limit]
    else:
        loans_to_process = new_loans

    summary = {
        "found": total_found,
        "selected": len(loans_to_process),
        "dry_run": dry_run,
        "approved": 0,
        "rejected": 0,
        "escalated": 0,
        "errors": 0,
        "items": [],
    }

    if not loans_to_process:
        print("No new loan applications pending workflow initiation.")
        return summary

    if dry_run:
        print(f"\n[DRY RUN] Found {total_found} new loan application(s). Processing limit: {len(loans_to_process)}.")
        for idx, loan in enumerate(loans_to_process, 1):
            app_id = loan["application_id"]
            name = loan["applicant_name"]
            amount = loan["loan_amount_requested"]
            income = loan["stated_annual_income"]
            print(f"  {idx}. [DRY RUN] {app_id}: {name} | Requested: ${amount:,.2f} | Income: ${income:,.2f}")
            summary["items"].append({
                "application_id": app_id,
                "applicant_name": name,
                "status": "DRY_RUN_ELIGIBLE",
            })
        print("Dry run completed. No workflow steps executed and no database state modified.\n")
        return summary

    print(f"\nInitiating workflow execution for {len(loans_to_process)} new loan application(s)...")
    graph = build_underwriting_graph(checkpointer=cp)

    for idx, loan in enumerate(loans_to_process, 1):
        app_id = loan["application_id"]
        name = loan["applicant_name"]
        print(f"\n[{idx}/{len(loans_to_process)}] Processing application {app_id} ({name})...")

        item = process_application(loan, graph=graph, app_db=db, initiator="WORKFLOW_SCHEDULER")

        if item.get("error"):
            summary["errors"] += 1
            print(f"  -> ERROR: Workflow execution error: {item['error']}")
        elif item["decision"] == "APPROVE":
            summary["approved"] += 1
            print(f"  -> Result: APPROVED ({item['status']})")
        elif item["decision"] in ["DENY", "REJECT_NO_CONSENT"]:
            summary["rejected"] += 1
            print(f"  -> Result: DENIED ({item['status']})")
        else:
            summary["escalated"] += 1
            print("  -> Result: ESCALATED (Pending Human Review)")

        summary["items"].append(item)

    print(
        f"\nExecution Summary: Processed={len(loans_to_process)} | "
        f"Approved={summary['approved']} | Rejected={summary['rejected']} | "
        f"Escalated={summary['escalated']} | Errors={summary['errors']}\n"
    )
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint for initiate_workflow."""
    raw_args = argv if argv is not None else sys.argv[1:]
    cleaned_args = normalize_cli_args(raw_args)

    parser = argparse.ArgumentParser(
        description="Kick-starts graph underwriting workflow for newly submitted loan applications."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and display eligible loans without executing workflow or modifying databases.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of new loan applications to process.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional path to database configuration INI file.",
    )

    parsed = parser.parse_args(cleaned_args)
    config = load_db_config(parsed.config) if parsed.config else None
    run_initiate_workflow(dry_run=parsed.dry_run, limit=parsed.limit, config=config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
