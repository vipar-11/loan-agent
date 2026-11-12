"""
test_manage_users.py
The user-management helper (manage_users.py -> loan_agent/auth/manage_users_cli.py): accounts for the three personas with the
EMPTY default password, idempotent re-runs, resets, enable/disable — and that the accounts it creates really sign in.
"""
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from api.dependencies import get_checkpointer, get_db
from api.main import create_app
from loan_agent.auth import LocalPasswordVerifier, SessionManager
from loan_agent.auth import users as user_directory
from loan_agent.auth.manage_users_cli import main
from loan_agent.config import AuthConfig
from loan_agent.db import ApplicationDatabase
from tests.conftest import create_user_with_password

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run(db, capsys, *argv):
    code = main(list(argv), db=db)
    return code, capsys.readouterr().out


class TestPersonaSetup:
    def test_defaults_create_the_three_personas_with_empty_passwords(self, test_app_db, capsys):
        code, out = run(test_app_db, capsys)
        assert code == 0
        users = {u["user_id"]: u for u in test_app_db.list_users()}
        assert {uid: u["role"] for uid, u in users.items()} == {
            "applicant": "APPLICANT", "underwriter": "UNDERWRITER", "admin": "ADMINISTRATOR",
        }
        assert all(u["password_hash"] is None and u["must_change_password"] and u["is_active"] for u in users.values())
        assert out.count("[CREATED]") == 3 and "EMPTY (set at first sign-in)" in out
        assert "EMPTY password" in out and "/login" in out

    def test_rerun_is_idempotent(self, test_app_db, capsys):
        run(test_app_db, capsys)
        user_directory.change_password(test_app_db, "admin", "", "Correct-Horse-9", AuthConfig())  # admin signed in
        code, out = run(test_app_db, capsys)
        assert code == 0 and out.count("[EXISTS]") == 3 and "[CREATED]" not in out
        assert test_app_db.get_user("admin")["password_hash"]  # untouched

    def test_custom_ids_and_names(self, test_app_db, capsys):
        code, out = run(test_app_db, capsys, "--applicant", "Jane.Doe", "--applicant-name", "Jane Doe",
                        "--underwriter", "uw.smith", "--underwriter-name", "Sam Smith", "--admin", "ops.lee")
        assert code == 0
        assert test_app_db.get_user("jane.doe")["display_name"] == "Jane Doe"
        assert test_app_db.get_user("uw.smith")["role"] == "UNDERWRITER"
        assert test_app_db.get_user("ops.lee")["display_name"] == "Administrator"
        assert test_app_db.get_user("applicant") is None  # defaults only when no persona is given

    def test_single_persona_adds_just_that_account(self, test_app_db, capsys):
        run(test_app_db, capsys)
        code, _ = run(test_app_db, capsys, "--underwriter", "uw.second")
        assert code == 0 and len(test_app_db.list_users()) == 4

    def test_role_conflict_and_invalid_ids_fail(self, test_app_db, capsys):
        run(test_app_db, capsys)
        code, out = run(test_app_db, capsys, "--underwriter", "admin", "--applicant", "bad id!")
        assert code == 1
        assert "already exists with the Administrator role" in out and "User IDs are 2-64 characters" in out
        assert test_app_db.get_user("admin")["role"] == "ADMINISTRATOR"


class TestMaintenance:
    def test_reset_restores_the_empty_password_and_signs_out(self, test_app_db, capsys):
        create_user_with_password(test_app_db, "uw.smith", "underwriter")
        cfg = AuthConfig()
        principal = LocalPasswordVerifier(test_app_db, cfg).verify("uw.smith", "Correct-Horse-9")
        session = SessionManager(test_app_db, cfg).issue(principal)

        code, out = run(test_app_db, capsys, "--reset", "UW.Smith")
        assert code == 0 and "[RESET] 'uw.smith'" in out and "EMPTY (set at first sign-in)" in out
        assert SessionManager(test_app_db, cfg).resolve(session.token) is None
        assert LocalPasswordVerifier(test_app_db, cfg).verify("uw.smith", "").password_change_required

    def test_disable_and_enable(self, test_app_db, capsys):
        run(test_app_db, capsys)
        code, out = run(test_app_db, capsys, "--disable", "applicant")
        assert code == 0 and "DISABLED" in out and test_app_db.get_user("applicant")["is_active"] is False
        assert not [u for u in test_app_db.list_users() if u["user_id"] == "admin" and not u["is_active"]]
        run(test_app_db, capsys, "--enable", "applicant")
        assert test_app_db.get_user("applicant")["is_active"] is True

    def test_unknown_user(self, test_app_db, capsys):
        code, out = run(test_app_db, capsys, "--reset", "nobody")
        assert code == 1 and "does not exist" in out

    def test_list(self, test_app_db, capsys):
        assert "No users are set up yet" in run(test_app_db, capsys, "--list")[1]
        run(test_app_db, capsys)
        code, out = run(test_app_db, capsys, "--list")
        assert code == 0 and "Loan Underwriter" in out and "[CREATED]" not in out


class TestAccountsSignIn:
    def test_provisioned_accounts_sign_in_with_an_empty_password(self, test_app_db, test_checkpointer, capsys):
        run(test_app_db, capsys)
        app = create_app(scheduler_enabled=False)
        app.dependency_overrides[get_db] = lambda: test_app_db
        app.dependency_overrides[get_checkpointer] = lambda: test_checkpointer
        for user_id, home in (("applicant", "/portal"), ("underwriter", "/desk"), ("admin", "/admin")):
            browser = TestClient(app)
            res = browser.post("/api/v1/auth/login", json={"user_id": user_id, "password": ""})
            assert res.status_code == 200 and res.json()["identity"]["password_change_required"] is True
            res = browser.post("/api/v1/auth/password", headers={"X-Requested-With": "test"},
                               json={"current_password": "", "new_password": "Correct-Horse-9"})
            assert res.status_code == 200 and res.json()["home_path"] == home
            assert browser.get(home).status_code == 200


class TestScript:
    def test_script_runs_against_the_configured_database(self, tmp_path):
        """The real entry point, in a subprocess, against a throw-away SQLite file (APP_DB_PATH)."""
        db_file = tmp_path / "app.db"
        env = {"APP_DB_PATH": str(db_file), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(PROJECT_ROOT)}
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "manage_users.py"), "--admin", "ops.lee"],
                                capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
        assert result.returncode == 0, result.stderr
        assert "[CREATED] Created 'ops.lee' (Administrator)" in result.stdout
        db = ApplicationDatabase(db_path=db_file)
        try:
            assert db.get_user("ops.lee")["role"] == "ADMINISTRATOR"
        finally:
            db.close()

    def test_help(self):
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "manage_users.py"), "--help"],
                                capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=120)
        assert result.returncode == 0 and "EMPTY default password" in result.stdout and "--reset" in result.stdout


class TestSandboxSetupScript:
    SCRIPT = PROJECT_ROOT / "setup_env.sh"

    def test_is_executable(self):
        import os
        assert os.access(self.SCRIPT, os.X_OK)
        assert self.SCRIPT.read_text().startswith("#!/usr/bin/env bash")

    def test_creates_the_default_persona_accounts(self):
        text = self.SCRIPT.read_text()
        assert "python manage_users.py )" in text  # run from the project root, default personas
        assert "setup_users" not in text
        assert subprocess.run(["bash", "-n", str(self.SCRIPT)]).returncode == 0

    def test_old_script_name_is_gone(self):
        assert not (PROJECT_ROOT / "setup_users.py").exists()
        assert (PROJECT_ROOT / "manage_users.py").exists()
