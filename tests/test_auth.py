"""
test_auth.py
Authentication end-to-end — NO dependency overrides: real users, real passwords, real sessions.

    core      password hashing & policy, user directory, password verifier (empty default password, lockout),
              session manager (hashed tokens, idle / absolute expiry, revocation)
    HTTP      sign-in via cookie (browser) and bearer token (API), first-sign-in password setup, CSRF defence,
              sign-out, password change, page redirects, and the authenticator chain as the SSO / token extension point
Role-based access control is covered in test_rbac.py.
"""
from datetime import datetime, timedelta
from typing import Generator, Optional

import pytest
from fastapi.testclient import TestClient

from api.dependencies import get_checkpointer, get_db
from api.main import create_app
from api.security import CSRF_HEADER, RequestAuthError
from loan_agent.auth import (
    AuthenticationError,
    LocalPasswordVerifier,
    PasswordChangeError,
    Principal,
    Role,
    SessionManager,
    hash_token,
)
from loan_agent.auth import passwords
from loan_agent.auth import users as user_directory
from loan_agent.config import AuthConfig, load_auth_config
from tests.conftest import bearer_headers, create_user_with_password

PASSWORD = "Correct-Horse-9"
CSRF = {CSRF_HEADER: "ApexWorkbench"}


# ===========================================================================
# Core
# ===========================================================================
class TestPasswords:
    def test_hash_and_verify(self):
        stored = passwords.hash_password("s3cret-pass")
        assert stored.startswith("scrypt$") and "s3cret-pass" not in stored
        assert passwords.verify_password("s3cret-pass", stored)
        assert not passwords.verify_password("s3cret-Pass", stored)
        assert passwords.hash_password("s3cret-pass") != stored  # salted

    def test_stored_cost_parameters_are_honoured(self, monkeypatch):
        stored = passwords.hash_password("pw-one-two")
        monkeypatch.setattr(passwords, "SCRYPT_N", 2 ** 9)  # cost raised later: old hashes still verify
        assert passwords.verify_password("pw-one-two", stored)

    @pytest.mark.parametrize("stored", [None, "", "garbage", "bcrypt$x$y", "scrypt$1$2$3$!!$!!"])
    def test_missing_or_malformed_hash_never_matches(self, stored):
        assert passwords.verify_password("anything", stored) is False
        assert passwords.verify_password("", stored) is False

    @pytest.mark.parametrize("new,error", [
        ("", "cannot be empty"), ("   ", "cannot be empty"), ("short", "at least 8"),
        ("x" * 300, "at most 256"), ("Some.Name", "same as the user ID"),
    ])
    def test_policy(self, new, error):
        with pytest.raises(passwords.PasswordPolicyError, match=error):
            passwords.validate_new_password(new, "some.name", 8)


class TestUserDirectory:
    def test_create_user_has_the_empty_default_password(self, test_app_db):
        user = user_directory.create_user(test_app_db, "  Some.Name ", "Loan Underwriter", "  Mrs.   Some   Name ")
        assert user["user_id"] == "some.name" and user["display_name"] == "Mrs. Some Name"
        assert user["role"] == "UNDERWRITER" and user["password_hash"] is None
        assert user["must_change_password"] is True and user["is_active"] is True
        assert user_directory.create_user(test_app_db, "ops", "admin")["display_name"] == "Administrator"

    @pytest.mark.parametrize("bad", ["", "a", "-lead", "has space", "semi;colon", "x" * 65, "ünï"])
    def test_user_id_format(self, bad, test_app_db):
        with pytest.raises(ValueError):
            user_directory.create_user(test_app_db, bad, Role.APPLICANT)

    def test_duplicate_and_unknown_role(self, test_app_db):
        user_directory.create_user(test_app_db, "applicant.one", Role.APPLICANT)
        with pytest.raises(ValueError, match="already exists"):
            user_directory.create_user(test_app_db, "APPLICANT.ONE", Role.APPLICANT)
        with pytest.raises(ValueError, match="Unknown role"):
            user_directory.create_user(test_app_db, "applicant.two", "superuser")

    def test_change_password_requires_the_current_one(self, test_app_db):
        cfg = AuthConfig()
        user_directory.create_user(test_app_db, "applicant.one", Role.APPLICANT)
        with pytest.raises(PasswordChangeError, match="current password is incorrect"):
            user_directory.change_password(test_app_db, "applicant.one", "not-empty", PASSWORD, cfg)
        user_directory.change_password(test_app_db, "applicant.one", "", PASSWORD, cfg)
        with pytest.raises(PasswordChangeError, match="current password is incorrect"):
            user_directory.change_password(test_app_db, "applicant.one", "", "Another-Pass-1", cfg)
        with pytest.raises(PasswordChangeError, match="must be different"):
            user_directory.change_password(test_app_db, "applicant.one", PASSWORD, PASSWORD, cfg)
        with pytest.raises(PasswordChangeError, match="at least"):
            user_directory.change_password(test_app_db, "applicant.one", PASSWORD, "short", cfg)
        user = user_directory.change_password(test_app_db, "applicant.one", PASSWORD, "Another-Pass-1", cfg)
        assert user["must_change_password"] is False and user["password_changed_at"]

    def test_external_accounts_cannot_use_local_passwords(self, test_app_db):
        test_app_db.create_user("sso.user", "SSO User", "UNDERWRITER", auth_provider="oidc:corp", external_subject="00u1")
        with pytest.raises(PasswordChangeError, match="external identity provider"):
            user_directory.change_password(test_app_db, "sso.user", "", PASSWORD, AuthConfig())
        with pytest.raises(AuthenticationError):
            LocalPasswordVerifier(test_app_db, AuthConfig()).verify("sso.user", "")


class TestLocalPasswordVerifier:
    @pytest.fixture
    def verifier(self, test_app_db):
        return LocalPasswordVerifier(test_app_db, AuthConfig(lockout_threshold=3, lockout_minutes=15))

    def test_empty_default_password_signs_in_restricted(self, verifier, test_app_db):
        user_directory.create_user(test_app_db, "uw.one", Role.UNDERWRITER, "Mrs. Some Name")
        principal = verifier.verify("UW.ONE", "")
        assert principal.user_id == "uw.one" and principal.role == Role.UNDERWRITER
        assert principal.password_change_required is True
        assert test_app_db.get_user("uw.one")["last_login_at"]

    def test_real_password(self, verifier, test_app_db):
        create_user_with_password(test_app_db, "uw.one", Role.UNDERWRITER, PASSWORD)
        assert verifier.verify("uw.one", PASSWORD).password_change_required is False
        for wrong in ("", "wrong", PASSWORD.lower()):
            with pytest.raises(AuthenticationError):
                verifier.verify("uw.one", wrong)

    def test_failures_do_not_reveal_whether_the_user_exists(self, verifier, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT, PASSWORD)
        messages = set()
        for user_id, pw in (("applicant.one", "wrong"), ("nobody", "wrong"), ("nobody", ""), ("bad id!", "x")):
            with pytest.raises(AuthenticationError) as exc:
                verifier.verify(user_id, pw)
            messages.add(str(exc.value))
        assert len(messages) == 1

    def test_disabled_account_cannot_sign_in(self, verifier, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT, PASSWORD)
        user_directory.set_active(test_app_db, "applicant.one", False)
        with pytest.raises(AuthenticationError):
            verifier.verify("applicant.one", PASSWORD)

    def test_lockout_after_repeated_failures(self, verifier, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT, PASSWORD)
        for _ in range(2):
            with pytest.raises(AuthenticationError):
                verifier.verify("applicant.one", "wrong")
        assert verifier.verify("applicant.one", PASSWORD)  # success before the threshold resets the count
        for _ in range(3):
            with pytest.raises(AuthenticationError):
                verifier.verify("applicant.one", "wrong")
        user = test_app_db.get_user("applicant.one")
        assert user["locked_until"] > datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with pytest.raises(AuthenticationError):  # locked: even the right password is refused
            verifier.verify("applicant.one", PASSWORD)
        test_app_db.update_user("applicant.one", {"locked_until": "2000-01-01 00:00:00"})  # lock expired
        assert verifier.verify("applicant.one", PASSWORD).user_id == "applicant.one"

    def test_reset_clears_the_lock_and_restores_the_empty_password(self, verifier, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT, PASSWORD)
        for _ in range(3):
            with pytest.raises(AuthenticationError):
                verifier.verify("applicant.one", "wrong")
        user_directory.reset_password(test_app_db, "applicant.one")
        assert verifier.verify("applicant.one", "").password_change_required is True


class TestSessionManager:
    @pytest.fixture
    def sessions(self, test_app_db):
        create_user_with_password(test_app_db, "uw.one", Role.UNDERWRITER, PASSWORD, "Mrs. Some Name")
        return SessionManager(test_app_db, AuthConfig(session_idle_minutes=30, session_max_hours=12))

    def _principal(self, db):
        return user_directory.principal_for(db.get_user("uw.one"), "password")

    def test_issue_and_resolve(self, sessions, test_app_db):
        issued = sessions.issue(self._principal(test_app_db))
        principal = sessions.resolve(issued.token)
        assert principal.user_id == "uw.one" and principal.display_name == "Mrs. Some Name"
        assert sessions.resolve("not-a-token") is None and sessions.resolve(None) is None
        assert sessions.resolve("x" * 500) is None

    def test_only_the_token_hash_is_stored(self, sessions, test_app_db):
        issued = sessions.issue(self._principal(test_app_db))
        rows = [dict(r) for r in test_app_db.conn.execute("SELECT * FROM auth_sessions").fetchall()]
        assert rows[0]["token_hash"] == hash_token(issued.token)
        assert issued.token not in str(rows)

    def test_idle_and_absolute_expiry(self, sessions, test_app_db):
        now = datetime.now()
        active, idle, old = (sessions.issue(self._principal(test_app_db)) for _ in range(3))
        assert sessions.resolve(active.token, now=now + timedelta(minutes=29))
        assert sessions.resolve(idle.token, now=now + timedelta(minutes=31)) is None  # idle timeout
        # kept busy right up to the end, but past the absolute lifetime
        test_app_db.touch_auth_session(hash_token(old.token), (now + timedelta(hours=12, minutes=5)).strftime("%Y-%m-%d %H:%M:%S"))
        assert sessions.resolve(old.token, now=now + timedelta(hours=12, minutes=6)) is None

    def test_activity_keeps_the_session_alive(self, sessions, test_app_db):
        issued = sessions.issue(self._principal(test_app_db))
        now = datetime.now()
        for minutes in (20, 40, 60, 80):  # each request within the idle window extends it
            assert sessions.resolve(issued.token, now=now + timedelta(minutes=minutes))

    def test_revocation_disable_and_role_change_apply_immediately(self, sessions, test_app_db):
        issued = sessions.issue(self._principal(test_app_db))
        user_directory.update_profile(test_app_db, "uw.one", role=Role.ADMINISTRATOR)
        assert sessions.resolve(issued.token).role == Role.ADMINISTRATOR
        user_directory.set_active(test_app_db, "uw.one", False)
        assert sessions.resolve(issued.token) is None
        user_directory.set_active(test_app_db, "uw.one", True)
        assert sessions.resolve(issued.token) is None  # disabling revoked it for good
        second = sessions.issue(self._principal(test_app_db))
        assert sessions.revoke(second.token) is True and sessions.resolve(second.token) is None
        assert sessions.revoke(second.token) is False


class TestAuthConfig:
    def test_defaults_and_overrides(self, tmp_path, monkeypatch):
        ini = tmp_path / "auth.ini"
        ini.write_text("[auth]\nsession_idle_minutes = 10\ncookie_secure = true\nlockout_threshold = 0\n")
        cfg = load_auth_config(ini)
        assert cfg.session_idle_minutes == 10 and cfg.cookie_secure is True
        assert cfg.lockout_threshold == 1  # clamped to a sane minimum
        assert cfg.session_max_hours == 12 and cfg.cookie_name == "apex_session"
        monkeypatch.setenv("AUTH_SESSION_IDLE_MINUTES", "5")
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
        cfg = load_auth_config(ini)
        assert cfg.session_idle_minutes == 5 and cfg.cookie_secure is False

    def test_shipped_auth_ini_is_valid(self):
        cfg = load_auth_config()
        assert cfg.password_min_length >= 8 and cfg.lockout_threshold >= 1


# ===========================================================================
# HTTP
# ===========================================================================
def make_app(db, checkpointer, **kwargs):
    app = create_app(scheduler_enabled=False, **kwargs)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_checkpointer] = lambda: checkpointer
    return app


@pytest.fixture
def app(test_app_db, test_checkpointer):
    application = make_app(test_app_db, test_checkpointer)
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def browser(app) -> Generator[TestClient, None, None]:
    """A browser: keeps cookies between requests."""
    with TestClient(app) as c:
        yield c


def login(client: TestClient, user_id: str, password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"user_id": user_id, "password": password})


PUBLIC_ROUTES = {
    ("GET", "/api/v1/health"), ("GET", "/api/v1/auth/providers"), ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/token"), ("POST", "/api/v1/auth/logout"), ("POST", "/api/v1/auth/signup"),
}


class TestAnonymousAccess:
    def test_every_api_route_requires_sign_in(self, browser):
        """Enumerated from the OpenAPI schema, so a new endpoint is protected unless deliberately made public."""
        schema = browser.get("/openapi.json").json()
        checked = 0
        for path, operations in schema["paths"].items():
            for method in operations:
                concrete = path.replace("{application_id}", "APP-X").replace("{run_id}", "1").replace("{batch_id}", "1")
                res = browser.request(method.upper(), concrete, json={})
                if (method.upper(), path) in PUBLIC_ROUTES:
                    assert res.status_code != 401, (method, path)
                    continue
                assert res.status_code == 401, (method, path, res.status_code)
                assert res.headers.get("WWW-Authenticate") == "Bearer"
                checked += 1
        assert checked >= 30

    def test_pages_redirect_to_sign_in(self, browser):
        for page in ("/portal", "/desk", "/admin"):
            res = browser.get(page, follow_redirects=False)
            assert res.status_code == 303 and res.headers["location"] == f"/login?next={page}"
        assert browser.get("/", follow_redirects=False).headers["location"] == "/login"

    def test_login_page(self, browser):
        html = browser.get("/login?next=/desk").text
        assert 'id="sign-in-form"' in html and 'id="password-form"' in html and 'data-next="/desk"' in html
        assert browser.get("/static/js/login.js").status_code == 200

    def test_sign_in_reveals_neither_the_default_password_nor_the_roles(self, browser):
        """Nothing a signing-in user sees hints that the default password is empty or lists the supported roles;
        the public API docs neither mention the empty password nor enumerate roles, and their sign-in endpoints
        name no role."""
        ui = {"page": browser.get("/login").text, "script": browser.get("/static/js/login.js").text,
              "providers": browser.get("/api/v1/auth/providers").text}
        for name, text in ui.items():
            lowered = text.lower()
            for leak in ("empty", "no password", "password yet", "leave the password"):
                assert leak not in lowered, (name, leak)
            # "New applicant? Create an account" invites self-service sign-up; it must not go further and name the
            # roles an administrator alone provisions.
            for leak in ("underwriter", "administrator", "admin"):
                assert leak not in lowered, (name, leak)

        spec = browser.get("/openapi.json").json()
        assert "empty" not in str(spec).lower()
        assert "enum" not in spec["components"]["schemas"]["UserIdentity"]["properties"]["role"]  # roles not listed
        for path, operations in spec["paths"].items():
            if path.startswith("/api/v1/auth") or path == "/api/v1/session":
                # /auth/signup documenting that it is applicant-only is fine (it is not a choice, let alone a
                # list of roles); underwriter / administrator accounts are never self-service and must not be
                # named here at all.
                for leak in ("underwriter", "administrator"):
                    assert leak not in str(operations).lower(), (path, leak)

    @pytest.mark.parametrize("evil", ["//evil.example", "https://evil.example", "/\\evil.example", "javascript:x"])
    def test_login_page_ignores_foreign_redirect_targets(self, browser, evil):
        assert 'data-next=""' in browser.get("/login", params={"next": evil}).text

    def test_sign_in_options(self, browser):
        assert browser.get("/api/v1/auth/providers").json() == [
            {"id": "local", "type": "password", "label": "User ID & password", "url": "/api/v1/auth/login"}
        ]

    def test_docs_offer_bearer_sign_in(self, browser):
        schemes = browser.get("/openapi.json").json()["components"]["securitySchemes"]
        assert schemes["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "/api/v1/auth/token"


class TestFirstSignIn:
    """The account from manage_users.py: user ID + EMPTY password, then a password must be chosen."""

    def test_full_flow(self, browser, test_app_db):
        user_directory.create_user(test_app_db, "uw.one", Role.UNDERWRITER, "Mrs. Some Name")

        res = login(browser, "uw.one", "")
        assert res.status_code == 200
        body = res.json()
        assert body["identity"]["password_change_required"] is True and "set a new password" in body["message"]
        cookie = res.headers["set-cookie"]
        assert "apex_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
        assert "Max-Age" not in cookie and "Secure" not in cookie  # browser-session cookie; secure per auth.ini

        # restricted: nothing but the password change
        blocked = browser.get("/api/v1/applications")
        assert blocked.status_code == 403 and blocked.headers["X-Apex-Password-Change-Required"] == "true"
        assert browser.get("/desk", follow_redirects=False).headers["location"] == "/login?next=/desk"
        assert browser.get("/api/v1/session").json()["password_change_required"] is True

        bad = browser.post("/api/v1/auth/password", json={"current_password": "", "new_password": "short"}, headers=CSRF)
        assert bad.status_code == 400 and "at least 8" in bad.json()["detail"]
        ok = browser.post("/api/v1/auth/password", json={"current_password": "", "new_password": PASSWORD}, headers=CSRF)
        assert ok.status_code == 200 and ok.json()["password_change_required"] is False
        assert ok.json()["home_path"] == "/desk" and ok.json()["pages"] == ["/desk"]

        # same session, now unrestricted
        assert browser.get("/api/v1/applications").status_code == 200
        assert browser.get("/desk").status_code == 200
        assert browser.get("/", follow_redirects=False).headers["location"] == "/desk"
        # and the empty password no longer works
        assert login(TestClient(browser.app), "uw.one", "").status_code == 401

    def test_api_client_first_sign_in(self, app, test_app_db):
        user_directory.create_user(test_app_db, "ops", Role.ADMINISTRATOR)
        api = TestClient(app)
        res = api.post("/api/v1/auth/token", data={"username": "ops", "password": ""})
        assert res.status_code == 200 and res.json()["password_change_required"] is True
        h = {"Authorization": f"Bearer {res.json()['access_token']}"}
        assert api.get("/api/v1/admin/scheduler", headers=h).status_code == 403
        assert api.post("/api/v1/auth/password", headers=h,
                        json={"current_password": "", "new_password": PASSWORD}).status_code == 200
        assert api.get("/api/v1/admin/scheduler", headers=h).status_code == 200


class TestSignIn:
    def test_wrong_credentials_get_one_generic_answer(self, browser, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT)
        answers = {(r.status_code, r.json()["detail"]) for r in (
            login(browser, "applicant.one", "wrong"), login(browser, "nobody", "wrong"), login(browser, "applicant.one", ""),
        )}
        assert len(answers) == 1 and next(iter(answers))[0] == 401
        assert "set-cookie" not in login(browser, "applicant.one", "wrong").headers

    def test_lockout_over_http(self, browser, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT)
        for _ in range(load_auth_config().lockout_threshold):
            assert login(browser, "applicant.one", "wrong").status_code == 401
        assert login(browser, "applicant.one", PASSWORD).status_code == 401

    def test_cookie_session(self, browser, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT, name="Mrs. Some Name")
        identity = login(browser, "applicant.one").json()["identity"]
        assert identity["role"] == "APPLICANT" and identity["home_path"] == "/portal"
        assert browser.get("/api/v1/session").json()["display_name"] == "Mrs. Some Name"
        portal = browser.get("/portal")
        assert portal.status_code == 200 and "Mrs. Some Name" in portal.text

    def test_signing_in_again_replaces_the_previous_session(self, browser, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT)
        login(browser, "applicant.one")
        first = browser.cookies.get("apex_session")
        login(browser, "applicant.one")
        assert browser.cookies.get("apex_session") != first
        stale = TestClient(browser.app, cookies={"apex_session": first})
        assert stale.get("/api/v1/session").status_code == 401

    def test_bearer_token(self, app, test_app_db):
        create_user_with_password(test_app_db, "uw.one", Role.UNDERWRITER)
        api = TestClient(app)
        res = api.post("/api/v1/auth/token", data={"username": "uw.one", "password": PASSWORD, "grant_type": "password"})
        body = res.json()
        assert body["token_type"] == "bearer" and 0 < body["expires_in"] <= 12 * 3600
        h = {"Authorization": f"Bearer {body['access_token']}"}
        assert api.get("/api/v1/applications", headers=h).status_code == 200
        # bearer requests are not subject to the browser CSRF rule
        assert api.post("/api/v1/applications/APP-X/checkout", headers=h).status_code == 404
        assert api.post("/api/v1/auth/token", data={"username": "uw.one", "password": "nope"}).status_code == 401

    def test_invalid_bearer_token_is_refused_even_with_a_valid_cookie(self, browser, test_app_db):
        create_user_with_password(test_app_db, "uw.one", Role.UNDERWRITER)
        login(browser, "uw.one")
        res = browser.get("/api/v1/applications", headers={"Authorization": "Bearer forged"})
        assert res.status_code == 401 and "invalid or has expired" in res.json()["detail"]


class TestCsrfDefence:
    def test_cookie_sessions_need_the_header_for_changes(self, browser, test_app_db):
        create_user_with_password(test_app_db, "uw.one", Role.UNDERWRITER)
        login(browser, "uw.one")
        assert browser.get("/api/v1/applications").status_code == 200  # reads: fine without
        res = browser.post("/api/v1/applications/APP-X/checkout")
        assert res.status_code == 403 and CSRF_HEADER in res.json()["detail"]
        assert browser.post("/api/v1/applications/APP-X/checkout", headers=CSRF).status_code == 404
        assert browser.post("/api/v1/auth/logout").status_code == 403  # no forced sign-out either
        assert browser.get("/api/v1/session").status_code == 200

    def test_workbench_scripts_send_the_header(self, browser):
        for js in ("/static/js/api.js", "/static/js/login.js"):
            assert "X-Requested-With" in browser.get(js).text


class TestSignOutAndPasswordChange:
    def test_sign_out(self, browser, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT)
        login(browser, "applicant.one")
        token = browser.cookies.get("apex_session")
        res = browser.post("/api/v1/auth/logout", headers=CSRF)
        assert res.status_code == 200 and 'apex_session=""' in res.headers["set-cookie"]
        assert browser.get("/api/v1/session").status_code == 401
        replay = TestClient(browser.app, cookies={"apex_session": token})  # a copied cookie is dead too
        assert replay.get("/api/v1/session").status_code == 401
        assert browser.post("/api/v1/auth/logout", headers=CSRF).status_code == 200  # idempotent

    def test_bearer_sign_out(self, app, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT)
        api = TestClient(app)
        h = bearer_headers(api, "applicant.one")
        assert api.post("/api/v1/auth/logout", headers=h).status_code == 200
        assert api.get("/api/v1/session", headers=h).status_code == 401

    def test_password_change_signs_out_other_sessions_only(self, app, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT)
        laptop, phone = TestClient(app), TestClient(app)
        login(laptop, "applicant.one")
        login(phone, "applicant.one")
        res = laptop.post("/api/v1/auth/password", headers=CSRF,
                          json={"current_password": PASSWORD, "new_password": "Brand-New-Pass-7"})
        assert res.status_code == 200
        assert laptop.get("/api/v1/session").status_code == 200
        assert phone.get("/api/v1/session").status_code == 401
        assert login(TestClient(app), "applicant.one", "Brand-New-Pass-7").status_code == 200

    def test_password_change_needs_a_session(self, browser):
        res = browser.post("/api/v1/auth/password", json={"current_password": "", "new_password": PASSWORD})
        assert res.status_code == 401

    def test_secure_cookie_when_configured(self, test_app_db, test_checkpointer):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT)
        app = make_app(test_app_db, test_checkpointer, auth_config=AuthConfig(cookie_secure=True, cookie_name="apex_s"))
        res = login(TestClient(app, base_url="https://testserver"), "applicant.one")
        assert "apex_s=" in res.headers["set-cookie"] and "Secure" in res.headers["set-cookie"]


class TestAuthenticatorChainIsTheExtensionPoint:
    """How SSO / IdP-token authentication plugs in: one more RequestAuthenticator; nothing downstream changes."""

    class FakeIdpTokenAuthenticator:
        """Stands in for e.g. an OIDC JWT validator: maps a verified token to a Principal."""
        name = "fake-idp"
        TOKENS = {"idp-token-uw": ("sub-42", "Idp Underwriter", Role.UNDERWRITER)}

        def authenticate(self, request, sessions) -> Optional[Principal]:
            token = request.headers.get("X-Idp-Token")
            if token is None:
                return None
            if token not in self.TOKENS:
                raise RequestAuthError(401, "IdP token rejected.")
            user_id, name, role = self.TOKENS[token]
            return Principal(user_id=user_id, display_name=name, role=role, auth_method="oidc:fake-idp")

    def test_custom_authenticator(self, test_app_db, test_checkpointer):
        from api.security import default_authenticators

        chain = [self.FakeIdpTokenAuthenticator(), *default_authenticators(AuthConfig())]
        app = make_app(test_app_db, test_checkpointer, authenticators=chain)
        api = TestClient(app)
        me = api.get("/api/v1/session", headers={"X-Idp-Token": "idp-token-uw"}).json()
        assert me["user_id"] == "sub-42" and me["auth_method"] == "oidc:fake-idp" and me["role"] == "UNDERWRITER"
        assert api.get("/api/v1/applications", headers={"X-Idp-Token": "idp-token-uw"}).status_code == 200
        assert api.get("/api/v1/admin/scheduler", headers={"X-Idp-Token": "idp-token-uw"}).status_code == 403
        assert api.get("/api/v1/applications", headers={"X-Idp-Token": "forged"}).status_code == 401
        assert api.get("/desk", headers={"X-Idp-Token": "idp-token-uw"}).status_code == 200
        # the built-in mechanisms still work behind it
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT)
        assert api.get("/api/v1/session", headers=bearer_headers(api, "applicant.one")).json()["user_id"] == "applicant.one"


class TestSecurityLogging:
    def test_failed_sign_ins_never_log_what_was_typed_for_unknown_accounts(self, browser, test_app_db, caplog):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT)
        with caplog.at_level("WARNING", logger="apex.auth"):
            login(browser, "Tr0ub4dor&3-typed-in-the-wrong-box", "x")
            login(browser, "applicant.one", "wrong")
        assert "Tr0ub4dor" not in caplog.text
        assert "unknown user ID" in caplog.text and "user applicant.one" in caplog.text

    def test_lockout_is_logged(self, browser, test_app_db, caplog):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT)
        with caplog.at_level("WARNING", logger="apex.auth"):
            for _ in range(load_auth_config().lockout_threshold):
                login(browser, "applicant.one", "wrong")
        assert "Account applicant.one locked until" in caplog.text
