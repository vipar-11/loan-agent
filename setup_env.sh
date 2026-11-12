#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup_env.sh — Developer sandbox bootstrap for the Apex Financial loan-agent.
#
#   1. Creates a Python virtual environment (./venv by default)
#   2. Activates it
#   3. Installs requirements.txt and requirements-dev.txt
#   4. Prepares the partner batch-intake folders (inbox/outbox/...) for every partner approved
#      in batch_intake.ini
#   5. Creates the default sign-in accounts for the three personas via manage_users.py
#      ('applicant', 'underwriter', 'admin'); existing accounts are left unchanged
#
# Usage (recommended — keeps the venv active in your current shell):
#   source ./setup_env.sh
#
# Also works when executed directly (the venv is created and populated, but a
# subprocess cannot activate a venv in your shell, so run `source venv/bin/activate`
# afterwards — the script reminds you):
#   ./setup_env.sh
#
# Options (environment variables):
#   VENV_DIR=.venv   source ./setup_env.sh     # custom venv location
#   PYTHON=python3.12 source ./setup_env.sh    # pick a specific interpreter
# ---------------------------------------------------------------------------

# Detect whether the script is being sourced (so we can `return` instead of `exit`).
_apex_sourced=0
if [ -n "${ZSH_VERSION:-}" ]; then
  case $ZSH_EVAL_CONTEXT in *:file) _apex_sourced=1;; esac
elif [ -n "${BASH_VERSION:-}" ]; then
  [ "${BASH_SOURCE[0]}" != "$0" ] && _apex_sourced=1
fi

_apex_fail() {
  echo "❌ $1" >&2
  if [ "$_apex_sourced" -eq 1 ]; then return 1; else exit 1; fi
}

# Resolve project root (directory containing this script) so it works from anywhere.
if [ -n "${BASH_SOURCE:-}" ]; then _apex_script="${BASH_SOURCE[0]}"; else _apex_script="$0"; fi
PROJECT_ROOT="$(cd "$(dirname "$_apex_script")" && pwd)"
VENV_DIR="${VENV_DIR:-$PROJECT_ROOT/venv}"
PYTHON="${PYTHON:-python3}"

echo "🏦 Apex Financial — loan-agent sandbox setup"
echo "   Project root : $PROJECT_ROOT"
echo "   Interpreter  : $($PYTHON --version 2>&1)  ($(command -v "$PYTHON"))"
echo "   Virtual env  : $VENV_DIR"
echo

# 1. Create the virtual environment (idempotent).
if [ ! -f "$VENV_DIR/bin/activate" ]; then
  echo "▶ Creating virtual environment..."
  "$PYTHON" -m venv "$VENV_DIR" || _apex_fail "Failed to create virtual environment with '$PYTHON -m venv'." || return 1
else
  echo "▶ Virtual environment already exists — reusing."
fi

# 2. Activate it.
echo "▶ Activating virtual environment..."
# shellcheck disable=SC1091
. "$VENV_DIR/bin/activate" || _apex_fail "Failed to activate $VENV_DIR." || return 1

# 3. Install dependencies from both requirements files.
echo "▶ Upgrading pip..."
python -m pip install --upgrade pip >/dev/null || _apex_fail "pip upgrade failed." || return 1

echo "▶ Installing requirements.txt..."
python -m pip install -r "$PROJECT_ROOT/requirements.txt" || _apex_fail "Failed installing requirements.txt." || return 1

echo "▶ Installing requirements-dev.txt..."
python -m pip install -r "$PROJECT_ROOT/requirements-dev.txt" || _apex_fail "Failed installing requirements-dev.txt." || return 1

# 4. Prepare the partner batch-intake folders (inbox/processing/archive/rejected/outbox), with an inbox and
#    outbox for every partner approved in batch_intake.ini ([approved_partners]) — so the folders the monitor
#    (and whatever FTP server is pointed at the inbox folders) expects are there from the very first run.
#    Re-running this is safe: existing folders and files are left untouched.
echo "▶ Preparing partner batch-intake folders (batch_intake.ini)..."
( cd "$PROJECT_ROOT" && python -c "
from loan_agent.config import load_intake_config
from api.services.batch_intake import ensure_folders

cfg = load_intake_config()
ensure_folders(cfg)
print(f'   Root          : {cfg.root_dir.resolve()}')
print(f'   Monitor       : {\"enabled\" if cfg.enabled else \"disabled\"}, every {cfg.poll_seconds:g}s')
if cfg.approved_partners:
    print('   Partners      :')
    for partner_id, name in sorted(cfg.approved_partners.items()):
        print(f'     {partner_id:<20} {name}')
        print(f'       inbox  : {(cfg.inbox_dir / partner_id).resolve()}   (point the partner\'s FTP account here)')
        print(f'       outbox : {(cfg.outbox_dir / partner_id).resolve()}   (results files for the partner to collect)')
else:
    print('   ⚠️  No approved partners configured — add them under [approved_partners] in batch_intake.ini.')
" ) || _apex_fail "Failed to prepare batch-intake folders (is batch_intake.ini present and valid?)." || return 1

# 5. Default sign-in accounts for the three personas, in the application database configured in dbconfig.ini.
#    Idempotent: accounts that already exist (and their passwords) are left untouched. Not fatal: with remote
#    storage the database may not be reachable yet — re-run `python manage_users.py` once it is.
echo "▶ Creating default sign-in accounts (manage_users.py)..."
( cd "$PROJECT_ROOT" && python manage_users.py ) || {
  echo "   ⚠️  Could not create the default accounts (is the application database in dbconfig.ini reachable?)."
  echo "      Re-run later with:  python manage_users.py"
}

echo
echo "✅ Sandbox ready."
echo "   Python : $(python --version 2>&1) at $(command -v python)"
echo
echo "   Next steps:"
echo "     cp .env.example .env            # (if present) add GROQ_API_KEY / OPENAI_API_KEY / LLM_API_KEY"
echo "     python manage_users.py --list   # sign-in accounts (applicant / underwriter / admin were created above)"
echo "     python run_api.py --reload      # FastAPI agentic service  -> http://localhost:8000/login"
echo "     streamlit run agentic_app.py    # retired: redirects to the FastAPI workbench"
echo "     streamlit run app.py            # Streamlit manual workbench"
echo "     pytest -m \"not integration\"     # test suite"
echo "     edit batch_intake.ini           # add/remove approved partners, then re-run this script"
if [ "$_apex_sourced" -eq 0 ]; then
  echo
  echo "   ℹ️  You executed this script, so the venv is active only inside it."
  echo "      Activate it in your shell with:  source \"$VENV_DIR/bin/activate\""
  echo "      (Next time, run:  source ./setup_env.sh  to activate automatically.)"
fi

unset _apex_sourced _apex_fail _apex_script
