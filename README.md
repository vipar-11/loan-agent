# 🏦 Apex Financial — Institutional Loan Underwriting & Approval Workbench

An enterprise-grade, FCRA & GLBA-compliant personal installment loan underwriting workbench and agentic decisioning platform. Built with **Streamlit**, **LangGraph**, and **Pydantic**, featuring deterministic financial ratio analysis, interactive bank cash flow classification, and human-in-the-loop credit adjudication.

---

## 🌟 Executive Summary

Apex Financial delivers a modern, high-trust credit decisioning platform designed for unsecured personal installment loans ($1,000 – $50,000, fixed 36-month term at 12.0% APR).

The system seamlessly combines:
1. **Interactive Dual-Role Web Workbench**:
   - **Loan Applicant Portal**: Real-time installment payment calculator, clean input state with contextual placeholders, regulatory consent capture, and 1-click demo persona autofill.
   - **Credit Underwriting Desk**: Modal-driven adjudication console, interactive bank cash flow categorizer with debit/credit-specific options, instant AI auto-classify shortcut, gated financial ratio worksheet, and a 1-click credit determination action panel with centered error validation.
2. **Deterministic Financial Calculation Engine (`calculator.py`)**:
   - SR 11-7 compliant pure Python functions for post-loan Debt-to-Income (DTI), verified payroll income, undisclosed BNPL debt detection, 90-day NSF event tallying, and liquidity cushion ratios.
3. **Multi-Agent LangGraph Pipeline (`graph_workflow.py`)**:
   - Autonomous multi-agent pipeline using LangGraph and Pydantic structured output for semantic bank reconciliation and dynamic policy evaluation without hardcoded credit policies.
4. **Single Source of Truth Credit Policy (`policy_v1.md`)**:
   - All underwriting tiers, knockout floors, and escalation gates are maintained in Markdown, guaranteeing zero policy hardcoding in business logic.

---

## 🏗️ System Architecture

```mermaid
flowchart TD
    subgraph UI["Streamlit Enterprise Workbench (app.py)"]
        Portal["👤 Applicant Portal\n- Live Payment Sizer\n- FCRA/GLBA Consent\n- Persona Autofill"]
        Desk["🛡️ Underwriter Review Desk\n- In-Memory Queue\n- Modal Dossier\n- Adjudication Memo"]
    end

    subgraph Data["Mock Data Services (mock_apis.py)"]
        Experian["🏛️ Experian Credit Bureau\n- FICO, Tradelines, Public Records"]
        Plaid["💳 Plaid 90-Day Bank Ledger\n- Inflows, Outflows, EOM Balances"]
    end

    subgraph Engine["Deterministic Math Engine (calculator.py)"]
        Calc["🧮 Pure Python Math (SR 11-7)\n- Verified Payroll & Income Variance\n- Undisclosed Debt (BNPL)\n- Post-Loan DTI & Liquidity Cushion"]
    end

    subgraph Agentic["LangGraph Pipeline (graph_workflow.py)"]
        Node1["Node 1: Consent Verification"]
        Node2["Node 2: Parallel Bureau & Bank Fetch"]
        Node3["Node 3: Bank Statement Reconciler Agent"]
        Node4["Node 4: Financial Ratio Calculator"]
        Node5["Node 5: Policy Underwriter Agent"]
        Node6["Node 6: Human-in-the-Loop Review"]
    end

    subgraph Policy["Policy Document"]
        PolMD["📜 policy_v1.md\n- Hard Knockout Criteria\n- Auto-Approval Tier\n- Human Escalation Gates"]
    end

    Portal -->|Submit Application| Desk
    Desk -->|Fetch Credit & Bank Data| Data
    Desk -->|Interactive Classification| Calc
    Data --> Engine
    Data --> Agentic
    PolMD -->|Dynamic Policy Context| Agentic
```

---

## 📁 Repository Structure

```
loan-agent/
├── pyproject.toml              # Modern PEP 517/621 packaging & pytest configuration
├── requirements.txt            # Production application dependencies
├── requirements-dev.txt        # Development and testing dependencies (pytest, pytest-cov)
├── policy_v1.md                # Apex Financial Credit Underwriting Policy (US-2026)
├── app.py                      # Manual credit underwriting workbench
├── agentic_app.py              # Autonomous LangGraph agentic underwriting workbench
├── .streamlit/
│   └── config.toml             # Streamlit Enterprise light theme configuration
├── loan_agent/                 # Core reusable Python package
│   ├── __init__.py             # Package exports & public API
│   ├── calculator.py           # Pure Python SR 11-7 Financial Ratio Calculator
│   ├── graph_workflow.py       # 6-Node LangGraph Agentic Underwriting Workflow
│   ├── mock_apis.py            # Experian & Plaid REST API Mock Providers & Personas
│   ├── schema.py               # Pydantic Structured Output Schemas (Agents 1 & 2)
│   └── state.py                # LangGraph LoanApplicationState TypedDict
└── tests/                      # Automated unit & integration test suite
    ├── __init__.py
    ├── conftest.py             # Shared pytest fixtures & mock payloads
    ├── test_calculator.py      # Unit tests for financial ratio algorithms
    ├── test_mock_apis.py       # Unit tests for bureau/bank APIs & pre-filtering
    ├── test_schema.py          # Unit tests for Pydantic schemas & state models
    ├── test_workflow.py        # Unit tests for LangGraph nodes, routing, fallback
    └── test_app_helpers.py     # Unit tests for presentation layer & formatters
```

---

## 🚀 Key Modules & Capabilities

### 1. Enterprise Workbench (`app.py`)
- **Light Enterprise Design System**: Styled with a crisp slate and blue palette (Stripe/Mercury style), featuring JetBrains Mono typography for financial metrics.
- **In-Memory Global State (`@st.cache_resource`)**: Data persists across browser refreshes ($F5$ / $\text{Cmd}+R$) while clearing completely on application server restarts—zero persistent disk artifacts or database debris.
- **Credit Tradelines Tab**: Clean audit view with `Tradeline ID` removed, showing creditor name, account type, monthly obligation, 12-month delinquency history, and standing.
- **Bank Cash Flow Tab**: 
  - `Txn Id` removed from the audit display table.
  - Transactions start unclassified by default.
  - Strictly filtered dropdowns: **Credit entries** only display deposit/income options (*Verified Payroll Income*, *One-Off Gift / Transfer*, *Internal Account Transfer*), while **Debit entries** only display expense options (*Known Bureau Debt*, *Undisclosed Debt / BNPL*, *NSF / Overdraft Fee*, *Bank Maintenance Fee*, *Other Living Expense*).
  - **`✨ Auto-Classify (Demo AI)`** shortcut button to instantly categorize all entries using heuristic rules.
- **Financial Ratios Tab**:
  - The **`🧮 Calculate Financial Ratios`** button is disabled until **100% of cash flow transactions** are classified.
  - Generates comprehensive underwriting ratios (*Post-Loan DTI*, *Verified Monthly Income*, *Payment Cushion Ratio*) and an audit table cross-referencing policy guidelines.
  - Dynamically resets when any transaction classification is modified, enabling seamless re-calculation.
- **Adjudication Modal & Action Panel**:
  - Encapsulated inside `st.form` to ensure **1-click approval or rejection** immediately upon typing the justification memorandum, preventing unwanted blur reruns.
  - Centered validation error messages spanning the full decision column.

---

### 2. Autonomous Agentic Workbench (`agentic_app.py`)
- **Direct Pipeline Trigger**: Submitting an application automatically triggers the 6-node LangGraph pipeline.
- **Intelligent Dual-Queue Routing**:
  - **`AUTO_APPROVED` & `AUTO_DENIED`**: Immediately bypass the pending queue and settle into the **Adjudicated / Completed Archive** with permanent audit logging.
  - **`ESCALATE` Only**: Halts at the LangGraph breakpoint before `human_review_node` and appears in the **Pending Loan Review Queue** for credit committee adjudication.
- **Auto-Populated Modal Dossier**:
  - **Bank Cash Flow Tab**: Classifications are auto-populated directly from graph state (`categorized_transactions` produced by the LLM Bank Statement Analyzer).
  - **Financial Ratios Tab**: Ratios are pre-calculated and auto-populated from graph state with on-demand `🔄 Recalculate Ratios` support.
  - **AI Agent Assessment Tab**: Provides full visibility into the AI's recommendation, confidence score, underwriter memo, policy citations, bank analyst narrative, and complete step-by-step execution log.
- **Human Override & LangGraph Resumption**:
  - Credit officers review the AI memo and enter their mandatory justification notes.
  - Clicking **`✅ Approve`** or **`❌ Reject`** updates the graph state (`human_reviewer_decision`, `human_reviewer_notes`) and resumes execution via `graph.invoke(None)`, finalizing as `HUMAN_APPROVED` or `HUMAN_DENIED`.

---

### 3. Financial Ratio Calculator (`calculator.py`)
All financial calculations are isolated in individual, pure functions:

| Function | Purpose | Formula / Logic |
| :--- | :--- | :--- |
| `get_fico_score()` | Extract FICO credit score | Extracted from credit report |
| `has_active_bankruptcy()` | Check active bankruptcy filings | `status == 'ACTIVE'` (Discharged filings ignored) |
| `get_total_delinquencies_12m()` | 12-Month delinquency tally | Sum of 30d + 60d + 90d+ late counts across tradelines |
| `count_nsf_overdraft_events()` | Punitive fee event count | Total 90-day NSF/overdraft ledger events |
| `calculate_verified_monthly_income()` | Bank-verified net payroll | $\frac{\sum \text{Verified Payroll Inflows}}{\text{Statement Months}}$ |
| `calculate_stated_monthly_income()` | Stated monthly gross income | $\frac{\text{Stated Gross Annual Income}}{12}$ |
| `calculate_income_variance_pct()` | Gross vs. net income variance | $\frac{|\text{Stated Monthly} - \text{Verified Monthly}|}{\text{Stated Monthly}} \times 100$ |
| `calculate_monthly_installment()` | Fixed-rate loan installment sizing | $P \times \frac{r(1+r)^n}{(1+r)^n - 1}$ ($r = 0.01$, $n = 36$) |
| `calculate_bureau_monthly_debt()` | Declared tradeline debt load | $\sum \text{Tradeline Monthly Payments}$ |
| `calculate_undisclosed_monthly_debt()` | Hidden debt obligations (BNPL) | $\frac{\sum \text{BNPL / Payday Outflows}}{\text{Statement Months}}$ |
| `calculate_total_post_loan_debt()` | Aggregate post-loan monthly debt | $\text{Bureau Debt} + \text{Hidden Debt} + \text{New Payment}$ |
| `calculate_post_loan_dti()` | Reconciled Post-Loan DTI % | $\frac{\text{Total Post-Loan Debt}}{\text{Verified Monthly Income}} \times 100$ |
| `calculate_avg_ending_balance()` | 90-Day average checking reserve | $\frac{\sum \text{EOM Ending Balances}}{\text{Months}}$ |
| `calculate_payment_cushion_ratio()` | Reserve liquidity multiple | $\frac{\text{Average Ending Balance}}{\text{New Monthly Loan Payment}}$ |
| `compute_financial_ratios()` | Master underwriting ratio engine | Bundles all metrics into a standardized dictionary |

---

### 4. Agentic LangGraph Underwriting Pipeline (`graph_workflow.py`)
A 6-node state machine orchestrating AI-driven credit analysis:
- **Node 1 (`verify_fcra_consent_node`)**: Regulatory gate ensuring explicit borrower consent before accessing bureau or banking data.
- **Node 2 (`fetch_external_data_parallel_node`)**: Parallel retrieval of Experian credit records and Plaid bank ledgers.
- **Node 3 (`bank_statement_analyzer_agent`)**: LLM-powered agent identifying verified income, undisclosed BNPL debt, and fee reversals.
- **Node 4 (`calculate_financial_ratios_node`)**: Pure Python mathematical calculation node.
- **Node 5 (`underwriter_agent_node`)**: Policy evaluation agent dynamically assessing `policy_v1.md` rules and generating ECOA-compliant adverse action notices when denying.
- **Node 6 (`human_review_node`)**: Human-in-the-loop review station for manual credit committee approval or sign-off.

---

## 👥 Demo Borrower Personas

The system includes 3 pre-configured demo borrower profiles accessible via the **`⚡ Autofill Demo Persona`** shortcut in the Applicant Portal:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Sarah Jenkins — Clean Auto-Approve (Prime)                                │
│    • Requested: $15,000 | Stated Income: $96,000/yr ($8,000/mo)             │
│    • FICO: 745 | Delinquencies: 0 | NSF Fees: 0                             │
│    • Public Records: Discharged Bankruptcy from 2019 (Compliant)            │
│    • Result: Passes all criteria, healthy DTI (< 36%), 4.2x cushion.        │
├─────────────────────────────────────────────────────────────────────────────┤
│ 2. Marcus Vance — Hard Knockout Denial (Subprime)                            │
│    • Requested: $20,000 | Stated Income: $60,000/yr ($5,000/mo)             │
│    • FICO: 580 (Violates Rule 2.1 < 620)                                    │
│    • Active Bankruptcy (Violates Rule 2.3) | 2x Delinquencies (Rule 2.3)    │
│    • 3x NSF Events in 90 Days (Violates Rule 2.4 > 2)                       │
│    • Result: Immediate ECOA Adverse Action Denial with specific citations.   │
├─────────────────────────────────────────────────────────────────────────────┤
│ 3. David Chen — Manual Underwriting Escalation (Nuanced / High-Value)        │
│    • Requested: $35,000 (Exceeds $25,000 Tier 1 Gate per Rule 4.2)          │
│    • Stated Income: $110,000/yr | FICO: 710                                 │
│    • Undisclosed BNPL detected (Klarna / Affirm payments)                   │
│    • Result: Routes to ESCALATE for human underwriter executive sign-off.   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## ⚙️ Installation & Setup

### Prerequisites
- Python 3.10, 3.11, 3.12, or 3.13
- Virtual environment (`venv` or `conda`)

### 1. Clone & Set Up Virtual Environment
```bash
cd /Users/vipar/Data/projects/loan-agent
python3 -m venv venv
source venv/bin/activate
```

### 2. Install Dependencies
```bash
# Core application dependencies
pip install -r requirements.txt

# Development and test dependencies (pytest, pytest-cov)
pip install -r requirements-dev.txt
```

### 3. Configure Environment Variables
Create or edit `.env` in the root directory to configure the LLM provider for the agentic workflow:
```bash
# LLM Provider Configuration (Defaults to Groq's OpenAI-compatible endpoint)
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=openai/gpt-oss-120b

# Your API Key (Groq, OpenAI, or compatible gateway)
GROQ_API_KEY=gsk_your_groq_api_key_here
# Alternatively: OPENAI_API_KEY=sk-... or LLM_API_KEY=...
```

---

## 🖥️ Running the Applications

Apex Financial provides two side-by-side implementations to demonstrate the evolution from manual credit underwriting to autonomous agentic decisioning:

### Option 1: Autonomous Agentic Workbench (`agentic_app.py`)

In this mode, the loan submission triggers the 6-node LangGraph pipeline. Automated decisions (`AUTO_APPROVED` / `AUTO_DENIED`) settle immediately into the archive, while complex files (`ESCALATE`) halt at the Human-in-the-Loop breakpoint and populate the Underwriter Review Queue with pre-populated AI assessments.

```bash
streamlit run agentic_app.py
```
Open your browser at `http://localhost:8501`.

#### Recommended Agentic Walkthrough:
1. Navigate to the **Applicant Portal**.
2. Click **`⚡ Autofill Demo Persona`**:
   - **Sarah Jenkins (Prime)**: Click Submit → AI Underwriter approves automatically per Tier 1 rules → application immediately moves to the **Adjudicated / Completed Archive** with status `AUTO_APPROVED`.
   - **Marcus Vance (Subprime)**: Click Submit → AI Underwriter detects knockout criteria (FICO < 620, DTI > 45%, 4 NSF fees) → application immediately moves to the **Adjudicated / Completed Archive** with status `AUTO_DENIED` and ECOA adverse action notices.
   - **David Chen (Nuanced / High-Value)**: Click Submit → loan amount ($35,000) and income variance (>15%) trigger Section 4 escalation → graph pauses execution before `human_review_node` → appears in the **Pending Loan Review Queue**.
3. Switch to the **Underwriter Desk** via the sidebar navigation.
4. Click **Review & Adjudicate ↗** on David Chen's escalated file:
   - **`🤖 AI Assessment` Tab**: Review the AI recommendation (`ESCALATE`), confidence score, detailed underwriter memorandum, policy citations, bank analyst narrative, and complete LangGraph execution trace.
   - **`💳 Bank Cash Flow` Tab**: Observe how all transactions are **auto-populated and pre-classified** by the AI Bank Statement Analyst agent.
   - **`📊 Financial Ratios` Tab**: Observe how all financial metrics (*Post-Loan DTI*, *Payment Cushion Ratio*, *Income Variance*) are **auto-populated from the graph state** without needing manual computation.
5. In the **Human Credit Officer Determination** action panel on the right:
   - Review the AI Agent Escalation briefing callout.
   - Enter your justification notes into **Credit Officer Justification** (e.g., *"Verified W-2 documentation confirms valid bonus structure; approved 41.8% DTI within near-prime tier"*).
   - Click **`✅ Approve`** (or **`❌ Reject`**).
   - The system updates graph state and resumes execution, finalizing the loan as `HUMAN_APPROVED`.
6. Navigate to the **Adjudicated / Completed Archive** to inspect the full audit trail and officer notes.

---

### Option 2: Manual Underwriting Workbench (`app.py`)

In this baseline mode, the credit officer performs the entire manual workflow from scratch (manual bank ledger classification, manual ratio calculation, and manual adjudication).

```bash
streamlit run app.py
```

#### Recommended Manual Walkthrough:
1. Navigate to the **Applicant Portal**.
2. Click **`⚡ Autofill Demo Persona`** and select **Sarah Jenkins** (or Marcus Vance / David Chen).
3. Observe how the **Calculated Installment** updates instantly with any change to the requested loan amount.
4. Check the FCRA authorization box and click **Submit Loan Application**.
5. Switch to the **Underwriter Desk** via the sidebar navigation.
6. Click **Review & Adjudicate ↗** on the submitted application to open the modal dossier.
7. Under the **Bank Cash Flow** tab, either classify entries manually or click **`✨ Auto-Classify (Demo AI)`**.
8. Navigate to the **Financial Ratios** tab and click **`🧮 Calculate Financial Ratios`** to generate the ratio scorecard.
9. Type your credit rationale into **Credit Officer Justification** and click **✅ Approve** or **❌ Reject**.
10. Inspect the completed record in the **Adjudicated / Completed Archive** audit trail.

---

## 🧪 Testing & Validation

The project includes both fast deterministic unit tests and live multi-agent LangGraph integration tests:

```bash
# 1. Run fast unit test suite (72 unit tests, zero API cost)
pytest -m "not integration"

# 2. Run unit tests with code coverage report
pytest --cov

# 3. Run live LangGraph integration tests across the 3 borrower personas:
#    - Sarah Jenkins (AUTO_APPROVED)
#    - Marcus Vance (AUTO_DENIED with ECOA adverse action notices)
#    - David Chen (ESCALATE, StateGraph breakpoint pause & human review resumption)
pytest tests/test_integration_workflow.py
```

---

## ⚖️ Compliance & Governance Standards

- **SR 11-7 (Supervisory Guidance on Model Risk Management)**: Deterministic, explainable mathematical calculations separate from generative LLM reasoning.
- **FCRA (Fair Credit Reporting Act)**: Explicit permission check enforces authorization prior to credit bureau or bank ledger pull.
- **GLBA (Gramm-Leach-Bliley Act)**: Safeguarding consumer financial information with ephemeral, in-memory processing.
- **ECOA (Equal Credit Opportunity Act / Reg B)**: Mandatory adverse action reasoning citations on all rejected loan files.
