"""
test_identity.py
Caller identity: establish_user_identity() — the signed-in user plus the browser tab it calls from (tab session in
X-Apex-Session-Id, used for review-checkout exclusivity) — and the authentication seam (resolve_principal) that every
sign-in mechanism plugs into. Sign-in itself is covered in test_auth.py, role enforcement in test_rbac.py.
"""
import uuid

from api.dependencies import SESSION_HEADER, resolve_principal
from loan_agent.auth import Principal, Role
from tests.test_api import client, escalated_app_id  # noqa: F401  (pytest fixtures; signed in as conftest.OPERATOR)


class TestTabSessionEstablishment:
    def test_first_call_without_header_issues_a_tab_session(self, client):
        res = client.get("/api/v1/session")
        assert res.status_code == 200
        body = res.json()
        token = res.headers[SESSION_HEADER]
        assert str(uuid.UUID(token)) == token  # a canonical UUID
        assert body["is_new_session"] is True
        assert body["user_id"] == "test.operator" and body["display_name"] == "Test Operator"
        assert body["role"] == "ADMINISTRATOR" and body["role_label"] == "Administrator"
        assert "tab_session" not in body and token not in res.text  # the tab token only travels in the header

    def test_subsequent_calls_with_the_header_keep_the_same_tab(self, client):
        token = client.get("/api/v1/session").headers[SESSION_HEADER]
        again = client.get("/api/v1/session", headers={SESSION_HEADER: token})
        assert again.json()["is_new_session"] is False
        assert again.headers[SESSION_HEADER] == token

    def test_each_new_tab_gets_a_different_tab_session(self, client):
        tokens = {client.get("/api/v1/session").headers[SESSION_HEADER] for _ in range(5)}
        assert len(tokens) == 5

    def test_malformed_token_is_replaced(self, client):
        for bad in ("not-a-uuid", "x" * 500, "' OR 1=1 --"):
            res = client.get("/api/v1/session", headers={SESSION_HEADER: bad})
            assert res.json()["is_new_session"] is True and res.headers[SESSION_HEADER] != bad

    def test_token_is_normalized(self, client):
        token = client.get("/api/v1/session").headers[SESSION_HEADER]
        res = client.get("/api/v1/session", headers={SESSION_HEADER: f"  {token.upper()}  "})
        assert res.headers[SESSION_HEADER] == token and res.json()["is_new_session"] is False


class TestEveryApiCallCarriesTheIdentity:
    def test_header_is_echoed_on_success_and_error_responses(self, client):
        token = client.get("/api/v1/session").headers[SESSION_HEADER]
        h = {SESSION_HEADER: token}
        for res in (
            client.get("/api/v1/personas", headers=h),
            client.get("/api/v1/applications", headers=h),
            client.get("/api/v1/admin/status-summary", headers=h),
            client.get("/api/v1/applications/APP-NOPE", headers=h),           # 404
            client.post("/api/v1/applications", json={}, headers=h),           # 422
        ):
            assert res.headers.get(SESSION_HEADER) == token, (res.request.url, res.status_code)

    def test_api_clients_are_not_issued_tab_sessions(self, client):
        """Only the workbench's tab bootstrap (GET /api/v1/session) issues one; API clients act as the user."""
        assert SESSION_HEADER not in client.get("/api/v1/applications").headers

    def test_every_page_displays_the_profile_menu(self, client):
        """Top-right, on every screen: avatar + dropdown with the signed-in user, Change password and Sign out."""
        for page in ("/portal", "/desk", "/admin"):
            html = client.get(page).text
            assert 'id="profile-menu"' in html and 'class="profile-avatar"' in html, page
            assert 'id="session-badge"' in html and 'id="session-label"' in html, page
            assert "Test Operator" in html and "test.operator" in html and "Administrator" in html, page
            assert 'id="btn-sign-out"' in html and 'id="btn-change-password"' in html, page
            assert "coming soon" not in html
        js = client.get("/static/js/api.js").text
        assert "renderProfileMenu" in js and "initialsFor" in js and "/auth/logout" in js

    def test_checkout_notice_sits_above_the_review_queue_not_in_the_profile_menu(self, client):
        desk = client.get("/desk").text
        menu = desk[desk.index('id="profile-menu"'):desk.index('<div class="app-shell">')]
        assert "review" not in menu.lower()
        notice, queue = desk.index('id="checkout-notice"'), desk.index('id="pending-queue"')
        assert notice < queue and "checked out to you in this browser tab" in desk
        assert desk.index('data-panel="pending"') < notice  # inside the pending-review tab
        for page in ("/portal", "/admin"):
            assert 'id="checkout-notice"' not in client.get(page).text

    def test_pages_do_not_issue_tab_sessions(self, client):
        """A page navigation cannot carry the header; the page's scripts establish the tab via the API."""
        for page in ("/portal", "/desk", "/admin"):
            assert SESSION_HEADER not in client.get(page).headers

    def test_identity_is_established_once_per_request(self, client, monkeypatch):
        import api.dependencies as deps

        calls = []
        original = deps.uuid.uuid4
        monkeypatch.setattr(deps.uuid, "uuid4", lambda: calls.append(1) or original())
        client.get("/api/v1/session")  # router-level + endpoint-level dependency
        assert len(calls) == 1


class TestAuthenticationPlugInSeam:
    def test_overriding_resolve_principal_swaps_the_identity_everywhere(self, client, escalated_app_id):
        """
        Every sign-in mechanism (password today; SSO / IdP tokens later) ends in resolve_principal(); downstream code
        — permissions, checkouts, audit actors — keys off the Principal. Simulated here with a dependency override.
        """
        who = {"current": Principal(user_id="sub-0001", display_name="Ms. Some Name", role=Role.UNDERWRITER,
                                    auth_method="oidc")}
        client.app.dependency_overrides[resolve_principal] = lambda: who["current"]

        res = client.post(f"/api/v1/applications/{escalated_app_id}/checkout")
        assert res.status_code == 200 and res.json()["checked_out_by"] == "Ms. Some Name"
        assert client.get("/api/v1/session").json()["auth_method"] == "oidc"

        who["current"] = Principal(user_id="sub-0002", display_name="Mr. Some Name", role=Role.UNDERWRITER,
                                   auth_method="oidc")
        blocked = client.post(f"/api/v1/applications/{escalated_app_id}/checkout")
        assert blocked.status_code == 409 and "checked out by another session" in blocked.json()["detail"]
        assert "Ms. Some Name" not in blocked.text
        trail = client.get(f"/api/v1/applications/{escalated_app_id}/audit-trail").json()
        assert any(e["event_type"] == "CHECKOUT_ACQUIRED" and e["actor"] == "Ms. Some Name (sub-0001)" for e in trail)

        # the role still applies: an SSO applicant cannot read the review queue
        who["current"] = Principal(user_id="sub-0003", display_name="A. Borrower", role=Role.APPLICANT,
                                   auth_method="oidc")
        assert client.get("/api/v1/applications").status_code == 403
