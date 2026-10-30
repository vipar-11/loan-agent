"""
Admin utility routines for loan management and auditability.
Powers list_loans and trace_loan CLI scripts.
"""
from typing import Optional, List, Dict, Any
import argparse
import sys

from loan_agent.config import DatabaseConfig, load_db_config
from loan_agent.db import ApplicationDatabase, get_application_database, normalize_status_filter
from loan_agent.checkpointer import get_langgraph_checkpointer
from loan_agent.graph_workflow import build_underwriting_graph


def normalize_cli_args(args: List[str]) -> List[str]:
    """Normalizes Unicode em-dashes, en-dashes, and curly quotes in CLI arguments."""
    stitched: List[str] = []
    accumulating = False
    acc_tokens: List[str] = []

    for a in args:
        if accumulating:
            acc_tokens.append(a)
            if "”" in a or '"' in a:
                stitched.append(" ".join(acc_tokens))
                accumulating = False
                acc_tokens = []
        elif "“" in a and "”" not in a:
            accumulating = True
            acc_tokens = [a]
        else:
            stitched.append(a)

    if accumulating and acc_tokens:
        stitched.append(" ".join(acc_tokens))

    normalized = []
    for arg in stitched:
        cleaned = arg
        if cleaned.startswith("—") or cleaned.startswith("–"):
            cleaned = "--" + cleaned[1:]
        if cleaned.startswith("--app_id="):
            cleaned = "--app_id=" + cleaned.split("=", 1)[1]
        cleaned = cleaned.replace("“", "").replace("”", "").replace('"', "")
        normalized.append(cleaned)
    return normalized



# ===========================================================================
# 1. LIST LOANS LOGIC
# ===========================================================================
def list_loans_action(
    status: Optional[str] = None,
    count_only: bool = False,
    app_db: Optional[ApplicationDatabase] = None,
    config: Optional[DatabaseConfig] = None,
) -> int:
    """
    Fetches and displays loan applications filtered by status or prints their count.
    Returns the count of matching items.
    """
    db = app_db or get_application_database(config)

    if count_only:
        total = db.count_applications(status=status)
        print(f"Count: {total}")
        return total

    apps = db.list_applications(status=status)
    if not apps:
        if status:
            print(f"No loan applications found matching status: '{status}'.")
        else:
            print("No loan applications found in database.")
        return 0

    header = f"{'Application ID':<16} | {'Applicant Name':<20} | {'Amount':<12} | {'Stated Income':<14} | {'Status':<18} | {'Submitted At':<19}"
    divider = "-" * len(header)

    print(f"\nFound {len(apps)} loan application(s):\n")
    print(divider)
    print(header)
    print(divider)

    for app in apps:
        app_id = app.get("application_id", "")
        name = app.get("applicant_name", "")
        amount = f"${app.get('loan_amount_requested', 0.0):,.2f}"
        income = f"${app.get('stated_annual_income', 0.0):,.2f}"
        st = app.get("status", "")
        submitted = app.get("submitted_at", "")
        print(f"{app_id:<16} | {name:<20} | {amount:<12} | {income:<14} | {st:<18} | {submitted:<19}")

    print(divider + "\n")
    return len(apps)


def main_list_loans(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint for list_loans."""
    raw_args = argv if argv is not None else sys.argv[1:]
    cleaned_args = normalize_cli_args(raw_args)

    parser = argparse.ArgumentParser(
        description="Query loan applications from persistent data store by status."
    )
    parser.add_argument(
        "--status",
        type=str,
        default=None,
        help="Filter status (new, pending processing, pending review, approved, or rejected).",
    )
    parser.add_argument(
        "--count",
        action="store_true",
        help="Print only the count of applications matching the criteria.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional path to database configuration INI file.",
    )

    parsed = parser.parse_args(cleaned_args)
    config = load_db_config(parsed.config) if parsed.config else None
    list_loans_action(status=parsed.status, count_only=parsed.count, config=config)
    return 0


# ===========================================================================
# 2. TRACE LOAN LOGIC (CONSOLIDATED AUDIT VIEW)
# ===========================================================================
def trace_loan_action(
    app_id: str,
    app_db: Optional[ApplicationDatabase] = None,
    checkpointer: Optional[Any] = None,
    config: Optional[DatabaseConfig] = None,
) -> bool:
    """
    Traces the history of a specific loan application by sourcing information
    from both the Application Database and the LangGraph checkpointer.
    Provides a consolidated view for a debugger / auditor / regulator.
    """
    clean_id = app_id.strip()
    db = app_db or get_application_database(config)
    cp = checkpointer or get_langgraph_checkpointer(config)

    app = db.get_application(clean_id)
    canonical_id = app["application_id"] if app else clean_id
    audit_trail = db.get_audit_trail(canonical_id)

    # LangGraph checkpoint state retrieval
    graph = build_underwriting_graph(checkpointer=cp)
    thread_config = {"configurable": {"thread_id": canonical_id}}
    state_snapshot = graph.get_state(thread_config)
    state_history = list(graph.get_state_history(thread_config))

    if not app and not state_history and not state_snapshot.values:
        print(f"\n[ERROR] Loan application '{clean_id}' was not found in either the Application Database or LangGraph Checkpointer.\n")
        return False

    sep_major = "=" * 80
    sep_minor = "-" * 80

    print(f"\n{sep_major}")
    print(f"CONSOLIDATED AUDIT & EXECUTION TRACE: {canonical_id}")
    print(sep_major)

    # 1. APPLICATION METADATA
    print("\n[1. APPLICATION METADATA & PROFILE]")
    print(sep_minor)
    if app:
        print(f"  • Application ID:       {app.get('application_id')}")
        print(f"  • Applicant Legal Name: {app.get('applicant_name')}")
        print(f"  • SSN (Last 4):         {app.get('ssn_last_4')}")
        print(f"  • Loan Purpose:         {app.get('loan_purpose')}")
        print(f"  • Loan Amount:          ${app.get('loan_amount_requested', 0.0):,.2f}")
        print(f"  • Stated Annual Income: ${app.get('stated_annual_income', 0.0):,.2f}")
        print(f"  • FCRA/GLBA Consent:    {'Granted' if app.get('fcra_consent') else 'Denied'}")
        print(f"  • Current Status:       {app.get('status')}")
        print(f"  • Initial Submitted At: {app.get('submitted_at')}")
        print(f"  • Last Updated At:      {app.get('updated_at')}")
    else:
        print("  (Record not found in Application Database)")

    # 2. ADJUDICATION OUTCOME
    adjudication = (app.get("adjudication") if app else None) or {}
    print("\n[2. ADJUDICATION OUTCOME]")
    print(sep_minor)
    if adjudication:
        print(f"  • Final Decision:       {adjudication.get('decision')}")
        print(f"  • Adjudicated By:       {adjudication.get('officer')}")
        print(f"  • Adjudicated At:       {adjudication.get('adjudicated_at')}")
        print(f"  • Confidence Score:     {adjudication.get('confidence_score', 'N/A')}")
        print(f"  • Policy Citations:     {', '.join(adjudication.get('policy_citations') or ['None'])}")
        adverse = adjudication.get("adverse_action_reasons") or []
        if adverse:
            print(f"  • Adverse Action:       {'; '.join(adverse)}")
        memo = adjudication.get("reason") or adjudication.get("agent_notes") or "N/A"
        print(f"  • Justification Memo:   {memo}")
    else:
        print("  (No final adjudication recorded yet)")

    # 3. GRAPH CHECKPOINTER STATE & FINANCIAL RATIOS
    graph_state = state_snapshot.values if state_snapshot and state_snapshot.values else (app.get("graph_state") if app else {}) or {}
    print("\n[3. UNDERWRITING FINANCIAL RATIOS & RECONCILIATION]")
    print(sep_minor)
    if graph_state:
        fico = graph_state.get("fico_score", "N/A")
        dti = f"{graph_state.get('post_loan_dti', 0.0):.2f}%" if graph_state.get("post_loan_dti") is not None else "N/A"
        income = f"${graph_state.get('verified_monthly_income', 0.0):,.2f}" if graph_state.get("verified_monthly_income") is not None else "N/A"
        cushion = f"{graph_state.get('payment_cushion_ratio', 0.0):.2f}x" if graph_state.get("payment_cushion_ratio") is not None else "N/A"
        var_pct = f"{graph_state.get('income_variance_pct', 0.0):.2f}%" if graph_state.get("income_variance_pct") is not None else "N/A"
        nsf = graph_state.get("nsf_count_90d", "N/A")
        delinq = graph_state.get("total_delinquencies_12m", "N/A")
        bk = "Yes" if graph_state.get("has_active_bankruptcy") else "No"

        print(f"  • FICO Credit Score:        {fico}")
        print(f"  • Active Bankruptcy:        {bk}")
        print(f"  • 12-Month Delinquencies:   {delinq}")
        print(f"  • 90-Day NSF Overdrafts:    {nsf}")
        print(f"  • Verified Monthly Income:  {income}")
        print(f"  • Income Variance %:        {var_pct}")
        print(f"  • Post-Loan DTI %:          {dti}")
        print(f"  • Liquidity Cushion Ratio:  {cushion}")
        if graph_state.get("bank_analyst_summary"):
            print(f"  • Bank Statement Summary:   {graph_state.get('bank_analyst_summary')}")
    else:
        print("  (Graph state snapshot not yet populated)")

    # 4. REGULATORY AUDIT TRAIL (CHRONOLOGICAL)
    print("\n[4. REGULATORY AUDIT TRAIL (APPLICATION DATABASE)]")
    print(sep_minor)
    if audit_trail:
        for idx, entry in enumerate(audit_trail, 1):
            ts = entry.get("timestamp")
            actor = entry.get("actor")
            evt = entry.get("event_type")
            from_st = entry.get("from_status") or "None"
            to_st = entry.get("to_status") or "None"
            dtl = entry.get("details") or ""
            print(f"  {idx}. [{ts}] Actor: {actor} | Event: {evt}")
            print(f"     Status Transition: {from_st} -> {to_st}")
            if dtl:
                print(f"     Details: {dtl}")
    else:
        print("  (No audit trail entries recorded)")

    # 5. LANGGRAPH CHECKPOINTER EXECUTION TRACE
    print("\n[5. LANGGRAPH EXECUTION LOG & THREAD HISTORY]")
    print(sep_minor)
    print(f"  • Thread ID:            {canonical_id}")
    print(f"  • Total Checkpoints:    {len(state_history)}")
    next_nodes = state_snapshot.next if state_snapshot else ()
    next_desc = ", ".join(next_nodes) if next_nodes else "None (Workflow Completed)"
    print(f"  • Next Pending Step:    {next_desc}")

    exec_log = graph_state.get("execution_log") or []
    if exec_log:
        print("  • Detailed Step Log:")
        for log_line in exec_log:
            print(f"     {log_line}")
    else:
        print("  (No execution log available in graph state)")

    print(f"\n{sep_major}\n")
    return True


def main_trace_loan(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint for trace_loan."""
    raw_args = argv if argv is not None else sys.argv[1:]
    cleaned_args = normalize_cli_args(raw_args)

    parser = argparse.ArgumentParser(
        description="Trace consolidated history of a specific loan application from Application DB and LangGraph."
    )
    parser.add_argument(
        "--app_id",
        "--app-id",
        dest="app_id",
        type=str,
        required=True,
        help="Application reference ID (e.g., APP-2026-001), case-insensitive.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional path to database configuration INI file.",
    )

    parsed = parser.parse_args(cleaned_args)
    config = load_db_config(parsed.config) if parsed.config else None
    success = trace_loan_action(app_id=parsed.app_id, config=config)
    return 0 if success else 1


if __name__ == "__main__":
    # If invoked directly, determine whether it's list_loans or trace_loan
    if "list_loans" in sys.argv[0]:
        sys.exit(main_list_loans())
    else:
        sys.exit(main_trace_loan())
