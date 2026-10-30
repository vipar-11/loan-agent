"""
Workflow initiation engine for newly submitted loan applications.
Executes the underwriting workflow with checkpointing in a distinct database.
"""
from datetime import datetime
from typing import Dict, Any, Optional, List
import argparse
import sys
from dotenv import load_dotenv

load_dotenv()

from loan_agent.config import DatabaseConfig, load_db_config
from loan_agent.db import ApplicationDatabase, get_application_database
from loan_agent.checkpointer import get_langgraph_checkpointer
from loan_agent.graph_workflow import build_underwriting_graph
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

        # Record initiation in audit trail
        db.add_audit_event(
            application_id=app_id,
            event_type="WORKFLOW_INITIATED",
            actor="WORKFLOW_SCHEDULER",
            from_status=loan.get("status", "NEW"),
            to_status="PENDING_PROCESSING",
            details="Approval workflow initiated via initiate_workflow",
        )

        initial_state: LoanApplicationState = {
            "application_id": app_id,
            "applicant_name": name,
            "ssn_last_4": str(loan["ssn_last_4"]),
            "stated_annual_income": float(loan["stated_annual_income"]),
            "loan_amount_requested": float(loan["loan_amount_requested"]),
            "loan_purpose": loan["loan_purpose"],
            "fcra_consent": bool(loan["fcra_consent"]),
        }

        thread_config = {"configurable": {"thread_id": app_id}}
        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            graph_output = graph.invoke(initial_state, config=thread_config)
            decision = graph_output.get("underwriting_decision", "ESCALATE")
            final_status = graph_output.get("final_status")

            if decision == "APPROVE":
                new_status = final_status or "AUTO_APPROVED"
                summary["approved"] += 1
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
                print(f"  -> Result: APPROVED ({new_status})")

            elif decision in ["DENY", "REJECT_NO_CONSENT"]:
                new_status = final_status or ("AUTO_DENIED" if decision == "DENY" else "REJECTED_NO_CONSENT")
                summary["rejected"] += 1
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
                print(f"  -> Result: DENIED ({new_status})")

            else:  # ESCALATE
                new_status = "PENDING_REVIEW"
                summary["escalated"] += 1
                adjudication_info = None
                event_type = "WORKFLOW_ESCALATED"
                audit_details = "Workflow halted before human review node; escalated for credit officer review."
                print("  -> Result: ESCALATED (Pending Human Review)")

            # Persist updated status and graph output
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

            summary["items"].append({
                "application_id": app_id,
                "applicant_name": name,
                "status": new_status,
                "decision": decision,
            })

        except Exception as exc:
            summary["errors"] += 1
            err_msg = f"Workflow execution error: {str(exc)}"
            print(f"  -> ERROR: {err_msg}")
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
                updates={
                    "status": "PENDING_REVIEW",
                    "graph_state": fallback_state,
                },
                actor="WORKFLOW_ENGINE",
                event_type="WORKFLOW_EXCEPTION",
                details=err_msg,
            )
            summary["items"].append({
                "application_id": app_id,
                "applicant_name": name,
                "status": "PENDING_REVIEW",
                "error": str(exc),
            })

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
