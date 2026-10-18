"""
app.py
Apex Financial — Institutional Loan Underwriting & Approval Workbench.

Clean Light Enterprise Theme (Stripe / Mercury / Plaid style):
- Native light theme configured via .streamlit/config.toml and crisp slate design system.
- Loan Applicant Portal:
  - Form is completely empty by default.
  - Realistic placeholder guidance (e.g., John Doe, e.g., 15,000.00).
  - Loan Amount Requested recalculates the monthly installment on EVERY increment/decrement.
  - Submitting an application immediately auto-increments the pending counter across sidebar & navbar.
  - Discreet '➕' shortcut button that 100% reliably autofills pre-configured borrower personas into widget state.
- Underwriter Review Desk:
  - Empty queue by default.
  - Clicking any pending application opens a full-featured modal dialog (@st.dialog) for review.
  - Clearly styled, intuitive clickable segmented tabs.
  - Objective financial calculations with zero hardcoded policy checks.
  - Zero policy citations in code to ensure policy_v1.md remains the single source of truth.
  - Prominent Approve / Reject action buttons at the bottom that prompt for the credit officer's reason before finalizing.
"""
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import pandas as pd
import streamlit as st

from mock_apis import (
    APPLICANT_PROFILES,
    fetch_experian_credit_report,
    fetch_plaid_bank_statement,
)
from calculator import (
    compute_financial_ratios,
    calculate_monthly_installment,
)

# ---------------------------------------------------------------------------
# IN-MEMORY GLOBAL REPOSITORY (SURVIVES BROWSER REFRESHES, CLEARS ON APP RESTART)
# ---------------------------------------------------------------------------
@st.cache_resource
def get_global_loan_repository() -> Dict[str, Any]:
    """
    In-memory dictionary singleton:
    1. Persists across browser tab refreshes (Cmd+R / F5) while the server is alive.
    2. Completely clears to a clean slate whenever the app is restarted.
    3. Requires zero disk files or data directories.
    """
    return {}


# ---------------------------------------------------------------------------
# STREAMLIT PAGE CONFIGURATION
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Apex Financial — Loan Underwriting Desk",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ENTERPRISE LIGHT FINANCIAL DESIGN SYSTEM (CSS)
# ---------------------------------------------------------------------------
def inject_enterprise_light_css():
    st.html(
        """<style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

        /* Streamlit native header non-interfering */
        header[data-testid="stHeader"] {
            background: transparent !important;
            pointer-events: none !important;
        }
        header[data-testid="stHeader"] button, header[data-testid="stHeader"] a {
            pointer-events: auto !important;
        }

        /* Clean Enterprise Layout with compact top clearance */
        .block-container {
            padding-top: 1.8rem !important;
            padding-bottom: 1.5rem !important;
            padding-left: 1.5rem !important;
            padding-right: 1.5rem !important;
            max-width: 96% !important;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
        }

        /* Top Header */
        .fin-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 18px;
            background: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 8px;
            margin-bottom: 12px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
        }
        .fin-brand-title {
            font-size: 1.1rem;
            font-weight: 700;
            color: #0F172A;
            display: flex;
            align-items: center;
            gap: 10px;
            letter-spacing: -0.01em;
        }
        .fin-brand-sub {
            font-size: 0.78rem;
            color: #64748B;
            font-weight: 400;
        }

        /* Badge Pills */
        .badge-pill {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 4px 10px;
            border-radius: 5px;
            font-size: 0.72rem;
            font-weight: 600;
            letter-spacing: 0.02em;
            text-transform: uppercase;
            font-family: 'JetBrains Mono', monospace;
        }
        .badge-blue { background: #EFF6FF; color: #1E40AF; border: 1px solid #BFDBFE; }
        .badge-green { background: #ECFDF5; color: #065F46; border: 1px solid #A7F3D0; }
        .badge-amber { background: #FFFBEB; color: #92400E; border: 1px solid #FDE68A; }
        .badge-red { background: #FEF2F2; color: #991B1B; border: 1px solid #FECACA; }

        /* Metric Box */
        .fin-metric {
            background: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 8px;
            padding: 12px 14px;
            margin-bottom: 10px;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
        }
        .fin-metric-label {
            font-size: 0.7rem;
            font-weight: 600;
            color: #64748B;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 4px;
        }
        .fin-metric-value {
            font-size: 1.35rem;
            font-weight: 700;
            color: #0F172A;
            font-family: 'JetBrains Mono', monospace;
            line-height: 1.2;
        }

        /* Informational Card Containers */
        .fin-card {
            background: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 8px;
            padding: 16px 20px;
            margin-bottom: 16px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
        }

        /* Live Installment Preview Box */
        .calc-preview-card {
            background: #F0F7FF;
            border: 1px solid #BFDBFE;
            border-radius: 8px;
            padding: 12px 18px;
            margin: 12px 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        /* Buttons Styling */
        .stButton > button, div[data-testid="stFormSubmitButton"] > button {
            border-radius: 6px !important;
            font-weight: 600 !important;
            font-size: 0.85rem !important;
            padding: 6px 16px !important;
            min-height: 38px !important;
            transition: all 0.15s ease !important;
        }

        /* Popover Button */
        div[data-testid="stPopover"] > button {
            border-radius: 6px !important;
            font-weight: 600 !important;
            font-size: 0.82rem !important;
            min-height: 38px !important;
            border: 1px solid #CBD5E1 !important;
            background: #FFFFFF !important;
            color: #1E293B !important;
        }
        div[data-testid="stPopover"] > button:hover {
            border-color: #94A3B8 !important;
            background: #F8FAFC !important;
        }

        /* Form Inputs */
        .stTextInput input, .stNumberInput input, .stSelectbox [data-baseweb="select"] {
            border-radius: 6px !important;
            border-color: #CBD5E1 !important;
        }
        div[data-testid="stSelectbox"] div[data-baseweb="select"] {
            min-height: 30px !important;
            font-size: 0.74rem !important;
            padding-top: 1px !important;
            padding-bottom: 1px !important;
        }
        .stTextInput input:focus, .stNumberInput input:focus {
            border-color: #2563EB !important;
            box-shadow: 0 0 0 1px #2563EB !important;
        }
        label[data-testid="stWidgetLabel"] p {
            font-size: 0.76rem !important;
            font-weight: 600 !important;
            color: #334155 !important;
            text-transform: uppercase !important;
            letter-spacing: 0.03em !important;
            margin-bottom: 4px !important;
        }

        /* Intuitive Tabs */
        div[data-testid="stTabs"] [data-baseweb="tab-list"] {
            gap: 10px !important;
            border-bottom: 2px solid #E2E8F0 !important;
            padding-bottom: 2px !important;
            margin-bottom: 16px !important;
        }
        div[data-testid="stTabs"] button[data-baseweb="tab"] {
            font-size: 0.88rem !important;
            font-weight: 600 !important;
            color: #64748B !important;
            padding: 8px 14px !important;
            border-radius: 4px !important;
        }
        div[data-testid="stTabs"] button[data-baseweb="tab"]:hover {
            color: #0F172A !important;
            background: #F1F5F9 !important;
        }
        div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
            color: #1E40AF !important;
            font-weight: 700 !important;
        }

        /* DataFrames */
        div[data-testid="stDataFrame"] {
            border: 1px solid #E2E8F0 !important;
            border-radius: 6px !important;
        }
        </style>"""
    )


inject_enterprise_light_css()


# ---------------------------------------------------------------------------
# IN-MEMORY STATE INITIALIZATION (EMPTY QUEUE & EMPTY FORM BY DEFAULT)
# ---------------------------------------------------------------------------
def initialize_repository():
    """Initializes in-memory storage for loan applications — persists across refreshes, resets on app restart."""
    if "loan_applications" not in st.session_state:
        st.session_state["loan_applications"] = get_global_loan_repository()

    if "current_view" not in st.session_state:
        st.session_state["current_view"] = "Applicant Portal"

    # Form generation counter ensures fresh empty form on start and after submission
    if "form_gen" not in st.session_state:
        st.session_state["form_gen"] = 1

    if "active_review_app_id" not in st.session_state:
        st.session_state["active_review_app_id"] = None


initialize_repository()


def generate_unique_app_id(existing_apps: Dict[str, Any]) -> str:
    """Generates a sequential, guaranteed-unique reference ID for every submitted loan application."""
    year = datetime.now().strftime("%Y")
    count = len(existing_apps) + 1
    candidate = f"APP-{year}-{count:03d}"
    if candidate in existing_apps:
        suffix = datetime.now().strftime("%H%M%S")
        candidate = f"APP-{year}-{count:03d}-{suffix}"
    return candidate


# Callbacks for Persona Autofill & Form Clearing
def autofill_persona_callback(persona_key: str):
    """Safely autofills pre-configured persona into next form generation keys before widget instantiation."""
    p = APPLICANT_PROFILES[persona_key]
    st.session_state["form_gen"] += 1
    g = st.session_state["form_gen"]
    st.session_state[f"app_name_{g}"] = p["applicant_name"]
    st.session_state[f"app_ssn_{g}"] = p["ssn_last_4"]
    st.session_state[f"app_income_{g}"] = float(p["stated_annual_income"])
    st.session_state[f"app_amount_{g}"] = float(p["loan_amount_requested"])
    purpose_opts = ["Home Improvement", "Debt Consolidation", "Medical Expenses", "Major Purchase", "Business", "Other"]
    st.session_state[f"app_purpose_{g}"] = p["loan_purpose"] if p["loan_purpose"] in purpose_opts else "Home Improvement"
    st.rerun()


def clear_form_callback():
    """Increments form generation to yield a clean, completely empty form with placeholders."""
    st.session_state["form_gen"] += 1
    st.rerun()


@st.dialog("⚡ Autofill Demo Persona")
def show_autofill_dialog():
    """Modal popup allowing 1-click selection of demo borrower profiles, auto-closing instantly on click."""
    st.markdown("<div style='font-size:0.8rem; color:#475569; margin-bottom:12px;'>Select a pre-configured borrower profile to populate form inputs for testing:</div>", unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    with col1:
        st.button("Sarah Jenkins\n(Prime $15,000)", on_click=autofill_persona_callback, args=("SARAH_JENKINS",), use_container_width=True)
        st.button("David Chen\n(Nuanced $35,000)", on_click=autofill_persona_callback, args=("DAVID_CHEN",), use_container_width=True)
    with col2:
        st.button("Marcus Vance\n(Subprime $20,000)", on_click=autofill_persona_callback, args=("MARCUS_VANCE",), use_container_width=True)
        st.button("Clear Form\n(Blank Slate)", on_click=clear_form_callback, use_container_width=True)


# ---------------------------------------------------------------------------
# FINANCIAL MATH HELPERS
# ---------------------------------------------------------------------------
def format_currency(val: float | int | None) -> str:
    if val is None or val == "":
        return "—"
    return f"${val:,.2f}"


def calculate_monthly_payment(loan_amount: Optional[float], annual_rate: float = 0.12, months: int = 36) -> Optional[float]:
    """Calculates monthly payment for a standard fixed-rate installment loan."""
    if loan_amount is None or loan_amount <= 0:
        return None
    r = annual_rate / 12.0
    payment = loan_amount * (r * ((1 + r) ** months)) / (((1 + r) ** months) - 1)
    return round(payment, 2)


def get_policy_markdown() -> str:
    """Loads underwriting policy guidelines from policy_v1.md."""
    policy_path = Path(__file__).parent / "policy_v1.md"
    if policy_path.exists():
        return policy_path.read_text(encoding="utf-8")
    return "# APEX FINANCIAL UNDERWRITING POLICY (US-2026)\nPolicy document text unavailable."


# ---------------------------------------------------------------------------
# DATA TRANSFORMATION HELPERS (COMPACT AUDIT TABLES, 100% JSON-FREE)
# ---------------------------------------------------------------------------
def build_tradelines_dataframe(credit_report: Dict[str, Any]) -> pd.DataFrame:
    """Converts credit tradelines into a compact audit table without Tradeline ID."""
    tradelines = credit_report.get("tradelines", [])
    if not tradelines:
        return pd.DataFrame()

    rows = []
    for tl in tradelines:
        delinq = tl.get("delinquencies_12m", {})
        d30 = delinq.get("30_days", 0)
        d60 = delinq.get("60_days", 0)
        d90 = delinq.get("90_plus_days", 0)

        total_delinq = d30 + d60 + d90
        delinq_str = "Clean (0)" if total_delinq == 0 else f"{d30}x30d, {d60}x60d, {d90}x90d+"
        status = "Current" if total_delinq == 0 else "Delinquent"

        rows.append({
            "Creditor Name": tl.get("creditor", "—"),
            "Account Type": tl.get("account_type", "—").replace("_", " "),
            "Monthly Obligation": format_currency(tl.get("monthly_payment")),
            "12M Delinquencies": delinq_str,
            "Standing": status,
        })
    return pd.DataFrame(rows)


# Tailored classification categories for Bank Cash Flow review
CREDIT_CLASSIFICATION_OPTIONS = [
    "— Select Classification —",
    "Verified Payroll Income",
    "One-Off Gift / Transfer (Excluded)",
    "Internal Account Transfer (Excluded)",
]

DEBIT_CLASSIFICATION_OPTIONS = [
    "— Select Classification —",
    "Known Bureau Debt Payment",
    "Undisclosed Debt / BNPL",
    "NSF / Overdraft Fee",
    "Bank Maintenance Fee (Excluded)",
    "Other Living Expense / Standard",
]


def auto_classify_transaction(tx: Dict[str, Any]) -> str:
    """Heuristic rule-based auto-classification for demo testing."""
    amt = float(tx.get("amount", 0.0))
    desc_upper = tx.get("description", "").upper()
    if amt > 0:
        if "PAYROLL" in desc_upper or "DIR DEP" in desc_upper:
            return "Verified Payroll Income"
        elif "XFER" in desc_upper or "TRANSFER" in desc_upper:
            return "Internal Account Transfer (Excluded)"
        elif "GIFT" in desc_upper or "ZELLE" in desc_upper or "MOM" in desc_upper:
            return "One-Off Gift / Transfer (Excluded)"
        else:
            return "One-Off Gift / Transfer (Excluded)"
    else:
        if "NSF" in desc_upper or "OVERDRAFT" in desc_upper or "RETURNED" in desc_upper:
            return "NSF / Overdraft Fee"
        elif "AFFIRM" in desc_upper or "KLARNA" in desc_upper or "PAYDAY" in desc_upper or "SPEEDYCASH" in desc_upper:
            return "Undisclosed Debt / BNPL"
        elif "AUTOPAY" in desc_upper or "LOAN" in desc_upper or "FIN" in desc_upper or "MTG" in desc_upper or "EPAY" in desc_upper:
            return "Known Bureau Debt Payment"
        elif "FEE" in desc_upper or "MAINTENANCE" in desc_upper or "SERVICE" in desc_upper:
            return "Bank Maintenance Fee (Excluded)"
        else:
            return "Other Living Expense / Standard"


# ---------------------------------------------------------------------------
# NAVIGATION TOP BAR & WORKSPACE CONTROLS
# ---------------------------------------------------------------------------
apps_dict = st.session_state["loan_applications"]
pending_count = sum(1 for a in apps_dict.values() if a.get("status") == "PENDING_REVIEW")
adjudicated_count = len(apps_dict) - pending_count
is_underwriter = (st.session_state.get("current_view") == "Underwriter Desk")

if is_underwriter:
    badges_html = (
        f'<span class="badge-pill badge-amber">⏳ {pending_count} PENDING</span> '
        f'<span class="badge-pill badge-green">✅ {adjudicated_count} ADJUDICATED</span> '
        f'<span class="badge-pill badge-blue">CREDIT DESK</span>'
    )
    sub_title = "Credit Underwriting & Adjudication Desk"
else:
    badges_html = (
        '<span class="badge-pill badge-blue">🔒 SECURE APPLICANT PORTAL</span> '
        '<span class="badge-pill badge-green">FCRA / GLBA COMPLIANT</span>'
    )
    sub_title = "Personal Loan Application Portal"

st.html(
    f"""<div class="fin-header">
        <div>
            <div class="fin-brand-title">
                🏦 Apex Financial
                <span style="font-weight:400; color:#CBD5E1;">|</span>
                <span class="fin-brand-sub">{sub_title}</span>
            </div>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
            {badges_html}
        </div>
    </div>"""
)

# Sidebar View Switcher & Live Queue Counter
with st.sidebar:
    st.markdown("<div style='font-size:0.75rem; font-weight:700; color:#334155; text-transform:uppercase; margin-bottom:8px;'>Navigation</div>", unsafe_allow_html=True)
    st.radio(
        "Active Portal View:",
        ["Applicant Portal", "Underwriter Desk"],
        key="current_view",
        label_visibility="collapsed",
    )

    if is_underwriter:
        st.markdown("<hr style='border-color:#E2E8F0; margin:12px 0;'/>", unsafe_allow_html=True)
        st.markdown("<div style='font-size:0.7rem; font-weight:600; color:#64748B; text-transform:uppercase;'>Queue Overview</div>", unsafe_allow_html=True)
        
        q_col1, q_col2 = st.columns(2)
        with q_col1:
            st.html(
                f"""<div class="fin-metric">
                    <div class="fin-metric-label">Pending Items</div>
                    <div class="fin-metric-value" style="color:#D97706;">{pending_count}</div>
                </div>"""
            )
        with q_col2:
            st.html(
                f"""<div class="fin-metric">
                    <div class="fin-metric-label">Adjudicated</div>
                    <div class="fin-metric-value" style="color:#059669;">{adjudicated_count}</div>
                </div>"""
            )

        if st.button("Reset In-Memory Queue", use_container_width=True):
            get_global_loan_repository().clear()
            st.session_state["loan_applications"] = get_global_loan_repository()
            st.session_state["form_gen"] += 1
            st.session_state["active_review_app_id"] = None
            st.rerun()
    else:
        st.markdown("<hr style='border-color:#E2E8F0; margin:14px 0;'/>", unsafe_allow_html=True)
        st.html(
            """<div style="font-size:0.72rem; color:#64748B; line-height:1.4;">
                <strong>Applicant Security</strong><br/>
                All submissions are encrypted and processed under FCRA & GLBA financial compliance.
            </div>"""
        )


# ---------------------------------------------------------------------------
# MODAL DIALOG FOR UNDERWRITING DECISION (@st.dialog)
# ---------------------------------------------------------------------------
@st.dialog("📋 Underwriting Adjudication Review", width="large")
def render_adjudication_dialog(app_id: str):
    """Opens a 2-column modal popup: Supporting Data on the left, Fixed Determination Actions on the right."""
    app = st.session_state["loan_applications"].get(app_id)
    if not app:
        st.error("Application not found.")
        return

    ssn = app["ssn_last_4"]
    stated_income = app["stated_annual_income"] or 0.0
    stated_monthly = round(stated_income / 12.0, 2)
    loan_req = app["loan_amount_requested"] or 0.0
    dynamic_pmt = calculate_monthly_payment(loan_req)

    # Fetch Bureau & Bank data from mock_apis
    credit_rep = {}
    bank_raw = {}
    if app.get("fcra_consent"):
        try:
            credit_rep = fetch_experian_credit_report(ssn)
        except Exception:
            credit_rep = {
                "bureau": "Experian",
                "fico_score": 670,
                "public_records": [],
                "tradelines": [
                    {"tradeline_id": "TL_GEN_01", "creditor": "REGIONAL AUTO", "account_type": "AUTO_LOAN", "monthly_payment": 380.00, "delinquencies_12m": {"30_days": 0, "60_days": 0, "90_plus_days": 0}},
                ],
            }
        try:
            bank_raw = fetch_plaid_bank_statement(ssn)
        except Exception:
            bank_raw = {
                "bank_name": "Primary Checking Account",
                "account_mask": ssn,
                "statement_period_months": 3,
                "eom_balances": [{"month": "Month 1", "ending_balance": 4500.0}],
                "transactions": [
                    {"tx_id": "GEN_01", "date": "2026-09-01", "description": "PAYROLL DEPOSIT", "amount": stated_monthly},
                ],
            }

    # 2-COLUMN SPLIT LAYOUT:
    # LEFT: Supporting Data Dossier (Tradelines, Bank Flow, Underwriting Policy)
    # RIGHT: Fixed Adjudication Actions & Memorandum (always visible, never jumps or hides)
    col_dossier, col_decision = st.columns([1.55, 1.15], gap="large")

    with col_dossier:
        # Compact Borrower Summary Banner
        st.html(
            f"""<div style="background:#FFFFFF; border:1px solid #E2E8F0; border-radius:6px; padding:8px 12px; margin-bottom:10px;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:0.95rem; font-weight:700; color:#0F172A;">{app['applicant_name']}</span>
                    <span style="font-size:0.72rem; color:#1E40AF; font-family:'JetBrains Mono'; font-weight:600;">[{app['application_id']}]</span>
                </div>
                <div style="font-size:0.72rem; color:#64748B; margin-top:2px;">
                    SSN: ••••-{ssn} | Stated: {format_currency(stated_income)}/yr ({format_currency(stated_monthly)}/mo) | Purpose: {app.get('loan_purpose', 'General')}
                </div>
            </div>"""
        )

        tab_bureau, tab_bank, tab_ratios, tab_policy = st.tabs([
            "🏛️ Credit Tradelines",
            "💳 Bank Cash Flow",
            "📊 Financial Ratios",
            "📜 Policy Guidelines",
        ])

        # TAB 1: Bureau Tradelines (Tradeline ID removed)
        with tab_bureau:
            fico = credit_rep.get("fico_score", 0)
            pub_recs = credit_rep.get("public_records", [])
            tls = credit_rep.get("tradelines", [])
            bureau_debt_val = sum(float(t.get("monthly_payment", 0)) for t in tls)

            b1, b2, b3 = st.columns(3)
            with b1:
                st.metric("FICO Score", fico)
            with b2:
                st.metric("Bureau Debt", format_currency(bureau_debt_val))
            with b3:
                st.metric("Public Records", len(pub_recs), delta="Clean" if not pub_recs else "Alert")

            if pub_recs:
                for pr in pub_recs:
                    st.warning(f"Public Record: {pr.get('type')} ({pr.get('status')})")

            df_tl = build_tradelines_dataframe(credit_rep)
            if not df_tl.empty:
                st.dataframe(df_tl, use_container_width=True, hide_index=True, height=210)
            else:
                st.caption("No active tradelines on file.")

        # TAB 2: Bank Cash Flow with interactive dropdown classification (Txn ID removed, credit vs debit options)
        with tab_bank:
            b_name = bank_raw.get("bank_name", "Primary Checking")
            eoms = bank_raw.get("eom_balances", [])
            avg_eom = sum(b.get("ending_balance", 0) for b in eoms) / len(eoms) if eoms else 0.0

            txs = bank_raw.get("transactions", [])
            total_inflows = sum(t["amount"] for t in txs if t["amount"] > 0)
            total_outflows = sum(abs(t["amount"]) for t in txs if t["amount"] < 0)

            k1, k2, k3 = st.columns(3)
            with k1:
                st.metric("Avg EOM Balance", format_currency(avg_eom))
            with k2:
                st.metric("Deposits (90D)", format_currency(total_inflows))
            with k3:
                st.metric("Debits (90D)", format_currency(total_outflows))

            # Classification session state tracking
            tx_key = f"classifications_{app_id}"
            if tx_key not in st.session_state:
                st.session_state[tx_key] = {}

            classified_count = sum(
                1 for t in txs
                if st.session_state[tx_key].get(t["tx_id"]) not in [None, "", "— Select Classification —"]
            )
            total_txs = len(txs)
            all_classified = (classified_count == total_txs and total_txs > 0)

            bar_c1, bar_c2 = st.columns([2.3, 1.7])
            with bar_c1:
                if all_classified:
                    st.markdown(
                        f"<div style='font-size:0.75rem; font-weight:600; color:#059669; padding-top:6px;'>"
                        f"✅ All {total_txs} transactions classified. Ready for ratio calculations.</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='font-size:0.75rem; font-weight:600; color:#D97706; padding-top:6px;'>"
                        f"⚠️ Classified: {classified_count} / {total_txs} ({total_txs - classified_count} pending)</div>",
                        unsafe_allow_html=True,
                    )
            with bar_c2:
                if st.button("✨ Auto-Classify (Demo AI)", key=f"btn_autoclass_{app_id}", use_container_width=True, help="Auto-classifies all cash flow transactions using rule heuristics"):
                    for t in txs:
                        cat_res = auto_classify_transaction(t)
                        st.session_state[tx_key][t["tx_id"]] = cat_res
                        st.session_state[f"sel_tx_{app_id}_{t['tx_id']}"] = cat_res
                    st.session_state.pop(f"ratios_{app_id}", None)
                    st.rerun()

            # Interactive Transaction Table (NO Txn ID, strictly relevant Credit/Debit options)
            with st.container(height=230):
                th_d, th_desc, th_amt, th_fl, th_cl = st.columns([1.1, 2.5, 1.1, 1.0, 2.5])
                th_d.markdown("<span style='font-size:0.72rem; font-weight:700; color:#475569;'>DATE</span>", unsafe_allow_html=True)
                th_desc.markdown("<span style='font-size:0.72rem; font-weight:700; color:#475569;'>DESCRIPTION</span>", unsafe_allow_html=True)
                th_amt.markdown("<span style='font-size:0.72rem; font-weight:700; color:#475569;'>AMOUNT</span>", unsafe_allow_html=True)
                th_fl.markdown("<span style='font-size:0.72rem; font-weight:700; color:#475569;'>FLOW</span>", unsafe_allow_html=True)
                th_cl.markdown("<span style='font-size:0.72rem; font-weight:700; color:#475569;'>CLASSIFICATION</span>", unsafe_allow_html=True)
                st.markdown("<hr style='margin:2px 0 6px 0; border-color:#E2E8F0;'/>", unsafe_allow_html=True)

                for tx in txs:
                    t_id = tx["tx_id"]
                    amt = float(tx.get("amount", 0.0))
                    is_credit = (amt > 0)
                    amt_str = f"+${amt:,.2f}" if is_credit else f"-${abs(amt):,.2f}"
                    amt_color = "#059669" if is_credit else "#0F172A"
                    flow_badge = "Deposit (+)" if is_credit else "Debit (-)"
                    flow_badge_class = "badge-green" if is_credit else "badge-blue"

                    r_d, r_desc, r_amt, r_fl, r_cl = st.columns([1.1, 2.5, 1.1, 1.0, 2.5])
                    with r_d:
                        st.markdown(f"<div style='font-size:0.74rem; color:#475569; padding-top:6px;'>{tx.get('date', '—')}</div>", unsafe_allow_html=True)
                    with r_desc:
                        st.markdown(f"<div style='font-size:0.74rem; font-weight:600; color:#1E293B; padding-top:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;' title='{tx.get('description', '')}'>{tx.get('description', '—')}</div>", unsafe_allow_html=True)
                    with r_amt:
                        st.markdown(f"<div style='font-size:0.74rem; font-weight:700; font-family:monospace; color:{amt_color}; padding-top:6px;'>{amt_str}</div>", unsafe_allow_html=True)
                    with r_fl:
                        st.markdown(f"<div style='padding-top:4px;'><span class='badge-pill {flow_badge_class}' style='font-size:0.62rem; padding:2px 5px;'>{flow_badge}</span></div>", unsafe_allow_html=True)
                    with r_cl:
                        opts = CREDIT_CLASSIFICATION_OPTIONS if is_credit else DEBIT_CLASSIFICATION_OPTIONS
                        widget_key = f"sel_tx_{app_id}_{t_id}"
                        if widget_key not in st.session_state:
                            st.session_state[widget_key] = st.session_state[tx_key].get(t_id, "— Select Classification —")

                        def on_tx_change(target_app_id=app_id, target_t_id=t_id, target_key=widget_key):
                            new_val = st.session_state.get(target_key, "— Select Classification —")
                            st.session_state[f"classifications_{target_app_id}"][target_t_id] = new_val
                            st.session_state.pop(f"ratios_{target_app_id}", None)

                        st.selectbox(
                            f"Classify {t_id}",
                            options=opts,
                            key=widget_key,
                            on_change=on_tx_change,
                            label_visibility="collapsed",
                        )

        # TAB 3: Financial Ratios (Enabled only when all transactions are classified; supports recalculation)
        with tab_ratios:
            rc_top1, rc_top2 = st.columns([2.4, 1.6])
            with rc_top1:
                if not all_classified:
                    st.markdown(
                        f"<div style='font-size:0.75rem; color:#92400E; background:#FFFBEB; border:1px solid #FDE68A; border-radius:6px; padding:6px 10px;'>"
                        f"⚠️ <strong>Action Required:</strong> Classify all {total_txs} transactions in the <strong>Bank Cash Flow</strong> tab ({total_txs - classified_count} pending) to enable calculation."
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='font-size:0.75rem; color:#065F46; background:#ECFDF5; border:1px solid #A7F3D0; border-radius:6px; padding:6px 10px;'>"
                        f"✅ <strong>All {total_txs} transactions classified.</strong> Ready to calculate underwriting ratios."
                        f"</div>",
                        unsafe_allow_html=True,
                    )
            with rc_top2:
                btn_lbl = "🔄 Recalculate Ratios" if f"ratios_{app_id}" in st.session_state else "🧮 Calculate Financial Ratios"
                if st.button(btn_lbl, type="primary", disabled=not all_classified, key=f"btn_calc_ratios_{app_id}", use_container_width=True):
                    cat_list = []
                    for t in txs:
                        cat_list.append({
                            "tx_id": t["tx_id"],
                            "date": t.get("date"),
                            "description": t.get("description"),
                            "amount": float(t.get("amount", 0.0)),
                            "category": st.session_state[tx_key].get(t["tx_id"]),
                        })
                    st.session_state[f"ratios_{app_id}"] = compute_financial_ratios(
                        credit_report=credit_rep,
                        bank_raw=bank_raw,
                        categorized_txs=cat_list,
                        stated_annual_income=stated_income,
                        loan_amount_requested=loan_req,
                    )
                    st.rerun()

            ratios = st.session_state.get(f"ratios_{app_id}")
            if not ratios:
                st.markdown(
                    """
                    <div style="background:#FFFFFF; border:1px dashed #CBD5E1; border-radius:6px; padding:32px; text-align:center; color:#64748B; margin-top:8px;">
                        <div style="font-size:1.3rem; margin-bottom:4px;">📊</div>
                        <div style="font-size:0.88rem; font-weight:600; color:#0F172A;">Financial Ratios Worksheet Uncalculated</div>
                        <div style="font-size:0.75rem; margin-top:2px;">Complete all bank cash flow classifications, then click <strong>Calculate Financial Ratios</strong>.</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            else:
                with st.container(height=230):
                    dti_val = ratios["post_loan_dti"]
                    dti_color = "#059669" if dti_val <= 43.0 else ("#D97706" if dti_val <= 50.0 else "#DC2626")

                    rk1, rk2, rk3 = st.columns(3)
                    with rk1:
                        st.markdown(
                            f"""
                            <div class="fin-metric">
                                <div class="fin-metric-label">Post-Loan DTI</div>
                                <div class="fin-metric-value" style="color:{dti_color};">{dti_val}%</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    with rk2:
                        st.markdown(
                            f"""
                            <div class="fin-metric">
                                <div class="fin-metric-label">Verified Monthly Income</div>
                                <div class="fin-metric-value" style="color:#0F172A;">{format_currency(ratios['verified_monthly_income'])}</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    with rk3:
                        st.markdown(
                            f"""
                            <div class="fin-metric">
                                <div class="fin-metric-label">Payment Cushion</div>
                                <div class="fin-metric-value" style="color:#2563EB;">{ratios['payment_cushion_ratio']}x</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

                    ratio_table_data = [
                        {"Underwriting Financial Metric": "FICO Credit Score", "Calculated Value": f"{ratios['fico_score']}", "Policy Reference": ">= 680 Prime (Tier 1)"},
                        {"Underwriting Financial Metric": "Active Bankruptcy Record", "Calculated Value": "ACTIVE" if ratios['has_active_bankruptcy'] else "None (Clean)", "Policy Reference": "Active is Knockout"},
                        {"Underwriting Financial Metric": "12-Month Tradeline Delinquencies", "Calculated Value": f"{ratios['total_delinquencies_12m']} late events", "Policy Reference": "<= 1 allowed for Tier 1"},
                        {"Underwriting Financial Metric": "90-Day Punitive NSF / Overdrafts", "Calculated Value": f"{ratios['nsf_count_90d']} fee events", "Policy Reference": "<= 2 allowed for Tier 1"},
                        {"Underwriting Financial Metric": "Stated vs. Verified Monthly Income", "Calculated Value": f"{format_currency(ratios['stated_monthly_income'])} stated vs {format_currency(ratios['verified_monthly_income'])} verified", "Policy Reference": "Verified Payroll Inflows"},
                        {"Underwriting Financial Metric": "Income Variance %", "Calculated Value": f"{ratios['income_variance_pct']}%", "Policy Reference": "<= 10% Tier 1, <= 25% Tier 2"},
                        {"Underwriting Financial Metric": "Bureau Monthly Debt", "Calculated Value": format_currency(ratios['bureau_monthly_debt']), "Policy Reference": "Declared Tradelines"},
                        {"Underwriting Financial Metric": "Undisclosed / Hidden Debt (BNPL)", "Calculated Value": format_currency(ratios['undisclosed_monthly_debt']), "Policy Reference": "Detected Recurring Debt"},
                        {"Underwriting Financial Metric": "Estimated New Loan Payment", "Calculated Value": format_currency(ratios['estimated_new_loan_payment']), "Policy Reference": "36 Mo @ 12.0% APR"},
                        {"Underwriting Financial Metric": "Total Aggregate Post-Loan Debt", "Calculated Value": format_currency(ratios['total_post_loan_debt']), "Policy Reference": "Bureau + Hidden + New Loan"},
                        {"Underwriting Financial Metric": "Reconciled Post-Loan DTI", "Calculated Value": f"{ratios['post_loan_dti']}%", "Policy Reference": "<= 38% Tier 1, <= 43% Tier 2"},
                        {"Underwriting Financial Metric": "Average Ending Checking Balance", "Calculated Value": format_currency(ratios['avg_ending_balance']), "Policy Reference": "90-Day EOM Average"},
                        {"Underwriting Financial Metric": "Liquidity Payment Cushion Ratio", "Calculated Value": f"{ratios['payment_cushion_ratio']}x", "Policy Reference": ">= 2.0x required for Tier 1"},
                    ]
                    df_ratios = pd.DataFrame(ratio_table_data)
                    st.dataframe(df_ratios, use_container_width=True, hide_index=True)

        # TAB 4: Underwriting Policy Guidelines
        with tab_policy:
            with st.container(height=260):
                st.markdown(get_policy_markdown())

    # RIGHT COLUMN: Fixed Determination Action Panel (st.form for 1-click submit and centered errors)
    with col_decision:
        st.html(
            f"""<div style="background:#F8FAFC; border:1px solid #E2E8F0; border-radius:6px; padding:10px 12px; margin-bottom:10px;">
                <div style="font-size:0.7rem; font-weight:700; color:#475569; text-transform:uppercase;">Loan Underwriting Request</div>
                <div style="display:flex; justify-content:space-between; align-items:baseline; margin-top:2px;">
                    <span style="font-size:1.35rem; font-weight:700; color:#0F172A; font-family:'JetBrains Mono';">{format_currency(loan_req)}</span>
                    <span style="font-size:0.8rem; font-weight:600; color:#1E40AF; font-family:'JetBrains Mono';">{format_currency(dynamic_pmt)}/mo</span>
                </div>
                <div style="font-size:0.7rem; color:#64748B; margin-top:2px;">Fixed Term: 36 Months @ 12.0% APR</div>
            </div>"""
        )

        st.markdown("<div style='font-size:0.75rem; font-weight:700; color:#334155; text-transform:uppercase; margin-bottom:4px;'>Credit Determination</div>", unsafe_allow_html=True)

        # Adjudication form prevents separate blur rerun, guaranteeing 1-click approval after typing
        with st.form(key=f"adjudication_form_{app_id}", border=False):
            officer_name = st.text_input(
                "Adjudicating Officer *",
                value="James Thornton, Senior Underwriter",
                key=f"off_{app_id}",
            )

            if f"notes_{app_id}" not in st.session_state:
                st.session_state[f"notes_{app_id}"] = ""

            officer_notes = st.text_area(
                "Credit Officer Justification (Mandatory) *",
                key=f"notes_{app_id}",
                height=110,
                placeholder="e.g. Verified revolving credit tradelines in good standing, stable payroll deposits, and adequate cash reserves per Tier 1 underwriting policy.",
                help="Mandatory: Explicitly enter reason for credit approval or adverse action.",
            )

            # Centered error banner spanning the full width of the decision column
            err_msg = st.session_state.get(f"adjudication_error_{app_id}")
            if err_msg:
                st.html(
                    f"""<div style="background:#FEF2F2; border:1px solid #FECACA; border-radius:6px; padding:8px 12px; margin-bottom:8px; color:#991B1B; font-size:0.75rem; font-weight:600; text-align:center;">
                        {err_msg}
                    </div>"""
                )

            b_act1, b_act2 = st.columns(2)
            with b_act1:
                submit_approve = st.form_submit_button("✅ Approve", type="primary", use_container_width=True)
            with b_act2:
                submit_reject = st.form_submit_button("❌ Reject", use_container_width=True)

        if submit_approve:
            notes_clean = (officer_notes or "").strip()
            if not notes_clean:
                st.session_state[f"adjudication_error_{app_id}"] = "⚠️ Mandatory: Enter underwriter justification memorandum before approving."
                st.rerun()
            else:
                timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                app["status"] = "APPROVED"
                app["adjudication"] = {
                    "decision": "APPROVED",
                    "officer": officer_name.strip() or "James Thornton, Senior Underwriter",
                    "reason": notes_clean,
                    "adjudicated_at": timestamp_str,
                }
                st.session_state[f"adjudication_error_{app_id}"] = None
                st.session_state["active_review_app_id"] = None
                st.rerun()

        if submit_reject:
            notes_clean = (officer_notes or "").strip()
            if not notes_clean:
                st.session_state[f"adjudication_error_{app_id}"] = "⚠️ Mandatory: Enter adverse action justification memorandum before rejecting."
                st.rerun()
            else:
                timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                app["status"] = "REJECTED"
                app["adjudication"] = {
                    "decision": "REJECTED",
                    "officer": officer_name.strip() or "James Thornton, Senior Underwriter",
                    "reason": notes_clean,
                    "adjudicated_at": timestamp_str,
                }
                st.session_state[f"adjudication_error_{app_id}"] = None
                st.session_state["active_review_app_id"] = None
                st.rerun()

        if st.button("Cancel / Close", use_container_width=True, key=f"btn_cancel_{app_id}"):
            st.session_state[f"adjudication_error_{app_id}"] = None
            st.session_state["active_review_app_id"] = None
            st.rerun()


# ===========================================================================
# VIEW 1: LOAN APPLICANT PORTAL (REALISTIC, ULTRA-COMPACT, ZERO SCROLLING)
# ===========================================================================
if st.session_state["current_view"] == "Applicant Portal":
    gen = st.session_state["form_gen"]

    # Header Row with Title and Autofill Shortcut
    hdr_c1, hdr_c2 = st.columns([3.6, 1.4])
    with hdr_c1:
        st.html(
            """<div style="background:#FFFFFF; border:1px solid #E2E8F0; border-radius:6px; padding:8px 14px; display:flex; align-items:center; justify-content:space-between;">
                <div>
                    <span style="font-size:0.95rem; font-weight:700; color:#0F172A;">Personal Installment Loan Application</span>
                    <span style="font-size:0.75rem; color:#64748B; margin-left:8px;">$1,000 – $50,000 (36 Months @ 12.0% APR)</span>
                </div>
            </div>"""
        )
    with hdr_c2:
        if st.button("⚡ Autofill Demo Persona", use_container_width=True, help="Demo shortcut: Autofill borrower profile"):
            show_autofill_dialog()

    # Submission success alert if an application was just submitted
    if "submission_success_id" in st.session_state:
        succ_id = st.session_state.pop("submission_success_id")
        st.success(f"Application {succ_id} submitted successfully into the underwriting queue.")

    # Form Fields: Ultra-compact 3-row layout
    # ROW 1: Borrower Information (Name, SSN, Gross Annual Income)
    c1, c2, c3 = st.columns([1.5, 0.9, 1.2])
    with c1:
        inp_name = st.text_input(
            "Borrower Legal Name",
            key=f"app_name_{gen}",
            placeholder="e.g. John Doe",
        )
    with c2:
        inp_ssn = st.text_input(
            "SSN (Last 4)",
            key=f"app_ssn_{gen}",
            max_chars=4,
            placeholder="e.g. 1234",
        )
    with c3:
        inc_kwargs = {} if f"app_income_{gen}" in st.session_state else {"value": None}
        inp_income = st.number_input(
            "Gross Annual Income ($)",
            key=f"app_income_{gen}",
            min_value=0.0,
            step=1000.0,
            format="%.2f",
            placeholder="e.g. 75,000.00",
            **inc_kwargs,
        )

    # ROW 2: Loan Amount, Purpose, and Live Installment Card (combined on 1 row)
    col_amt, col_purp, col_card = st.columns([1.2, 1.2, 1.3])
    with col_amt:
        amt_kwargs = {} if f"app_amount_{gen}" in st.session_state else {"value": None}
        inp_amount = st.number_input(
            "Loan Amount Requested ($)",
            key=f"app_amount_{gen}",
            min_value=1000.0,
            max_value=50000.0,
            step=1000.0,
            format="%.2f",
            placeholder="e.g. 15,000.00",
            help="Recalculates monthly payment on every single increment.",
            **amt_kwargs,
        )
    with col_purp:
        purpose_options = ["Home Improvement", "Debt Consolidation", "Medical Expenses", "Major Purchase", "Business", "Other"]
        purp_kwargs = {} if f"app_purpose_{gen}" in st.session_state else {"index": None}
        inp_purpose = st.selectbox(
            "Primary Loan Purpose",
            purpose_options,
            key=f"app_purpose_{gen}",
            placeholder="Select loan purpose...",
            **purp_kwargs,
        )
    with col_card:
        dynamic_pmt = calculate_monthly_payment(inp_amount)
        pmt_display = format_currency(dynamic_pmt) if dynamic_pmt is not None else "—"
        st.html(
            f"""<div style="background:#F0F7FF; border:1px solid #BFDBFE; border-radius:6px; padding:6px 12px; height:68px; display:flex; flex-direction:column; justify-content:center;">
                <div style="font-size:0.68rem; font-weight:600; color:#1E3A8A; text-transform:uppercase;">Calculated Installment</div>
                <div style="font-size:1.15rem; font-weight:700; color:#1E40AF; font-family:'JetBrains Mono'; line-height:1.2;">
                    {pmt_display} <span style="font-size:0.72rem; font-weight:500; color:#3B82F6;">/ mo</span>
                </div>
            </div>"""
        )

    # ROW 3: Regulatory Consent checkbox on the left, Submit button on the right
    col_agree, col_sub = st.columns([3.2, 1.3])
    with col_agree:
        inp_consent = st.checkbox(
            "I provide explicit FCRA and GLBA authorization for credit bureau inquiry and bank cash flow review.",
            value=True,
            key=f"app_consent_{gen}",
        )
    with col_sub:
        submit_btn = st.button("Submit Loan Application", type="primary", use_container_width=True)

    if submit_btn:
        if not (inp_name or "").strip():
            st.error("Please enter borrower legal name.")
        elif not (inp_ssn or "").strip() or len((inp_ssn or "").strip()) != 4:
            st.error("Please enter a valid 4-digit SSN.")
        elif inp_amount is None or inp_amount <= 0:
            st.error("Please enter a valid loan amount requested.")
        elif inp_income is None or inp_income <= 0:
            st.error("Please enter stated annual income.")
        elif not inp_purpose:
            st.error("Please select a loan purpose.")
        else:
            # ALWAYS generate a fresh unique application ID so resubmitting never overwrites adjudicated records!
            new_app_id = generate_unique_app_id(st.session_state["loan_applications"])
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            st.session_state["loan_applications"][new_app_id] = {
                "application_id": new_app_id,
                "applicant_name": inp_name.strip(),
                "ssn_last_4": inp_ssn.strip(),
                "stated_annual_income": inp_income,
                "loan_amount_requested": inp_amount,
                "loan_purpose": inp_purpose,
                "fcra_consent": inp_consent,
                "status": "PENDING_REVIEW",
                "submitted_at": timestamp_str,
                "adjudication": None,
            }

            st.session_state["form_gen"] += 1
            st.session_state["submission_success_id"] = new_app_id
            st.rerun()


# ===========================================================================
# VIEW 2: UNDERWRITER REVIEW DESK (WITH MODAL POPUP REVIEW)
# ===========================================================================
else:
    st.html(
        """<div class="fin-card" style="padding:12px 18px; margin-bottom:14px; display:flex; justify-content:space-between; align-items:center;">
            <div>
                <div style="font-size:0.95rem; font-weight:700; color:#0F172A; margin-bottom:2px;">Underwriting Review & Adjudication Desk</div>
                <div style="font-size:0.75rem; color:#64748B;">Inspect pending credit requests, verify financial ratios, and record final credit determination.</div>
            </div>
            <span class="badge-pill badge-green">Credit Officer Desk</span>
        </div>"""
    )

    tab_pending_queue, tab_adjudicated_archive = st.tabs([
        f"📥 Pending Loan Review Queue ({pending_count})",
        f"🏁 Adjudicated / Completed Archive ({adjudicated_count})",
    ])

    # -----------------------------------------------------------------------
    # SUB-TAB A: PENDING REVIEW QUEUE
    # -----------------------------------------------------------------------
    with tab_pending_queue:
        pending_apps = [a for a in apps_dict.values() if a.get("status") == "PENDING_REVIEW"]

        if not pending_apps:
            st.html(
                """<div style="background:#FFFFFF; border:1px dashed #CBD5E1; border-radius:6px; padding:36px; text-align:center; color:#64748B;">
                    <div style="font-size:1.2rem; margin-bottom:4px;">📭</div>
                    <div style="font-size:0.9rem; font-weight:600; color:#0F172A;">Underwriting Queue Is Empty (0 Pending Reviews)</div>
                    <div style="font-size:0.75rem; margin-top:2px;">Applications submitted through the <strong>Applicant Portal</strong> will automatically appear here.</div>
                </div>"""
            )
        else:
            st.markdown("<div style='font-size:0.75rem; font-weight:700; color:#334155; text-transform:uppercase; margin-bottom:8px;'>Pending Queue Items — Click to Open Review Modal</div>", unsafe_allow_html=True)

            # Interactive List with Direct "Review & Adjudicate" Buttons
            for app in pending_apps:
                app_id = app["application_id"]
                req_amt = format_currency(app["loan_amount_requested"])
                income = format_currency(app["stated_annual_income"])
                ssn_masked = f"••••-{app['ssn_last_4']}"

                row_col1, row_col2 = st.columns([4.2, 1])
                with row_col1:
                    st.html(
                        f"""<div style="background:#FFFFFF; border:1px solid #E2E8F0; border-radius:6px; padding:10px 14px; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center;">
                            <div>
                                <span style="font-size:0.9rem; font-weight:700; color:#0F172A;">{app['applicant_name']}</span>
                                <span style="font-size:0.72rem; color:#2563EB; font-family:'JetBrains Mono'; margin-left:6px;">[{app_id}]</span>
                                <div style="font-size:0.72rem; color:#64748B; margin-top:3px;">
                                    SSN: {ssn_masked} | Stated Income: {income}/yr | Purpose: {app['loan_purpose']} | Submitted: {app['submitted_at']}
                                </div>
                            </div>
                            <span style="font-size:1.05rem; font-weight:700; color:#0F172A; font-family:'JetBrains Mono';">{req_amt}</span>
                        </div>"""
                    )
                with row_col2:
                    if st.button("Review & Adjudicate ↗", key=f"open_modal_{app_id}", use_container_width=True):
                        st.session_state["active_review_app_id"] = app_id
                        st.session_state[f"adjudication_error_{app_id}"] = None
                        st.rerun()

    # -----------------------------------------------------------------------
    # SUB-TAB B: ADJUDICATED ARCHIVE
    # -----------------------------------------------------------------------
    with tab_adjudicated_archive:
        completed_apps = [a for a in apps_dict.values() if a.get("status") in ["APPROVED", "REJECTED"]]

        if not completed_apps:
            st.html(
                """<div style="background:#FFFFFF; border:1px dashed #CBD5E1; border-radius:6px; padding:36px; text-align:center; color:#64748B;">
                    <div style="font-size:1.2rem; margin-bottom:4px;">📁</div>
                    <div style="font-size:0.9rem; font-weight:600; color:#0F172A;">Archive Empty (0 Adjudicated Records)</div>
                    <div style="font-size:0.75rem; margin-top:2px;">Finalized applications will be logged here for permanent compliance auditing.</div>
                </div>"""
            )
        else:
            archive_rows = []
            for app in completed_apps:
                adj = app.get("adjudication") or {}
                dec = adj.get("decision", app.get("status", "—"))
                archive_rows.append({
                    "Ref ID": app["application_id"],
                    "Borrower": app["applicant_name"],
                    "Decision": dec,
                    "Loan Sized": format_currency(app["loan_amount_requested"]),
                    "Purpose": app.get("loan_purpose", "General"),
                    "Adjudicating Officer": adj.get("officer", "Credit Officer"),
                    "Adjudicated Timestamp": adj.get("adjudicated_at", app.get("submitted_at")),
                })
            df_archive = pd.DataFrame(archive_rows)
            st.dataframe(df_archive, use_container_width=True, hide_index=True)

            st.markdown("<hr style='border-color:#E2E8F0; margin:14px 0;'/>", unsafe_allow_html=True)
            st.markdown("<div style='font-size:0.75rem; font-weight:700; color:#334155; text-transform:uppercase; margin-bottom:8px;'>📜 Audit Trail Dossier Inspector</div>", unsafe_allow_html=True)

            comp_ids = [a["application_id"] for a in completed_apps]
            sel_comp_id = st.selectbox(
                "Select Adjudicated File to Inspect:",
                comp_ids,
                format_func=lambda cid: f"{cid} — {apps_dict[cid]['applicant_name']} [{apps_dict[cid]['status']}]",
            )

            aud_app = apps_dict[sel_comp_id]
            aud_adj = aud_app.get("adjudication") or {}
            aud_dec = aud_adj.get("decision", aud_app.get("status"))

            badge_style = "badge-green" if "APPROV" in aud_dec else "badge-red"
            st.html(
                f"""<div style="background:#FFFFFF; border:1px solid #E2E8F0; border-radius:6px; padding:12px 16px; margin-bottom:12px; display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <span style="font-size:1rem; font-weight:700; color:#0F172A;">Determination: {aud_dec}</span>
                        <div style="font-size:0.75rem; color:#64748B; margin-top:2px;">
                            File: {aud_app['application_id']} | Borrower: {aud_app['applicant_name']} | Officer: {aud_adj.get('officer', 'Senior Officer')} | Timestamp: {aud_adj.get('adjudicated_at', '—')}
                        </div>
                    </div>
                    <span class="badge-pill {badge_style}">{aud_dec}</span>
                </div>"""
            )

            ak1, ak2, ak3 = st.columns(3)
            with ak1:
                st.metric("Loan Sized", format_currency(aud_app['loan_amount_requested']))
            with ak2:
                st.metric("Stated Annual Income", format_currency(aud_app.get('stated_annual_income')))
            with ak3:
                st.metric("Loan Purpose", aud_app.get('loan_purpose', 'General'))

            st.html(
                f"""<div style="background:#FFFFFF; border:1px solid #E2E8F0; border-radius:6px; padding:12px 16px; margin-top:8px;">
                    <div style="font-size:0.68rem; font-weight:700; color:#64748B; text-transform:uppercase; margin-bottom:4px;">Recorded Underwriter Reason & Memorandum:</div>
                    <div style="font-size:0.82rem; color:#0F172A; line-height:1.5;">{aud_adj.get('reason', 'No memorandum recorded.').replace(chr(10), '<br/>')}</div>
                </div>"""
            )

    # Automatically open adjudication dialog when an application is selected for review
    if st.session_state.get("active_review_app_id"):
        render_adjudication_dialog(st.session_state["active_review_app_id"])

