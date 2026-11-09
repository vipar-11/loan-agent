"""
test_identity.py
Caller identity: establish_user_identity() — anonymous per-browser-tab sessions carried in X-Apex-Session-Id,
and the dependency-override seam that a future authentication mechanism plugs into.
"""
import uuid

from api.dependencies import SESSION_HEADER, establish_user_identity
from api.schemas import UserIdentity
from tests.test_api import client, escalated_app_id  # noqa: F401  (pytest fixtures)


class TestSessionEstablishment:
    def test_first_call_without_header_issues_a_new_session(self, client):
        res = client.get("/api/v1/session")
        assert res.status_code == 200
        body = res.json()
        token = res.headers[SESSION_HEADER]
        assert str(uuid.UUID(token)) == token  # a canonical UUID
        assert body["user_id"] == token and body["is_new_session"] is True
        assert body["auth_method"] == "anonymous-session"
        assert body["display_name"].startswith("Session ") and token[:8].upper() not in body["display_name"]

    def test_subsequent_calls_with_the_header_keep_the_same_identity(self, client):
        token = client.get("/api/v1/session").headers[SESSION_HEADER]
        first_label = client.get("/api/v1/session", headers={SESSION_HEADER: token}).json()
        again = client.get("/api/v1/session", headers={SESSION_HEADER: token})
        assert again.json()["user_id"] == token and again.json()["is_new_session"] is False
        assert again.json()["display_name"] == first_label["display_name"]
        assert again.headers[SESSION_HEADER] == token

    def test_each_new_tab_gets_a_different_identity(self, client):
        tokens = {client.get("/api/v1/session").headers[SESSION_HEADER] for _ in range(5)}
        assert len(tokens) == 5

    def test_malformed_token_is_replaced(self, client):
        for bad in ("not-a-uuid", "x" * 500, "' OR 1=1 --"):
            res = client.get("/api/v1/session", headers={SESSION_HEADER: bad})
            assert res.json()["is_new_session"] is True and res.headers[SESSION_HEADER] != bad

    def test_token_is_normalized(self, client):
        token = client.get("/api/v1/session").headers[SESSION_HEADER]
        res = client.get("/api/v1/session", headers={SESSION_HEADER: f"  {token.upper()}  "})
        assert res.json()["user_id"] == token and res.json()["is_new_session"] is False


class TestEveryApiCallCarriesTheIdentity:
    def test_header_is_echoed_on_success_and_error_responses(self, client):
        token = client.get("/api/v1/session").headers[SESSION_HEADER]
        h = {SESSION_HEADER: token}
        for res in (
            client.get("/api/v1/health", headers=h),
            client.get("/api/v1/applications", headers=h),
            client.get("/api/v1/admin/status-summary", headers=h),
            client.get("/api/v1/applications/APP-NOPE", headers=h),           # 404
            client.post("/api/v1/applications", json={}, headers=h),           # 422
        ):
            assert res.headers.get(SESSION_HEADER) == token, (res.request.url, res.status_code)

    def test_headerless_api_call_is_issued_a_session(self, client):
        res = client.get("/api/v1/health")
        assert uuid.UUID(res.headers[SESSION_HEADER])

    def test_every_page_displays_the_profile_menu(self, client):
        """Top-right, on every screen: a round avatar + dropdown, styled like a post-login account icon."""
        for page in ("/portal", "/desk", "/admin"):
            html = client.get(page).text
            assert 'id="profile-menu"' in html and 'class="profile-avatar"' in html, page
            assert 'id="session-badge"' in html and 'id="session-label"' in html, page
            assert 'id="profile-dropdown"' in html and "Sign in (coming soon)" in html, page
        js = client.get("/static/js/api.js").text
        assert "renderProfileMenu" in js and "initialsFor" in js

    def test_pages_do_not_issue_sessions(self, client):
        """A page navigation cannot carry the header; the page's scripts establish the session via the API."""
        for page in ("/portal", "/desk", "/admin"):
            assert SESSION_HEADER not in client.get(page).headers

    def test_identity_is_established_once_per_request(self, client, monkeypatch):
        import api.dependencies as deps

        calls = []
        original = deps.uuid.uuid4
        monkeypatch.setattr(deps.uuid, "uuid4", lambda: calls.append(1) or original())
        client.get("/api/v1/applications")  # router-level + endpoint-level dependency
        assert len(calls) == 1


class TestAuthenticationPlugInSeam:
    def test_overriding_establish_user_identity_swaps_the_identity_everywhere(self, client, escalated_app_id):
        """
        Future authentication replaces establish_user_identity(); downstream code keys off user_id/display_name.
        Simulated here with FastAPI dependency overrides.
        """
        who = {"current": UserIdentity(user_id="sub-0001", display_name="Ms. Some Name", auth_method="oidc")}
        client.app.dependency_overrides[establish_user_identity] = lambda: who["current"]

        res = client.post(f"/api/v1/applications/{escalated_app_id}/checkout")
        assert res.status_code == 200 and res.json()["checked_out_by"] == "Ms. Some Name"
        assert SESSION_HEADER not in res.headers  # authenticated identities are not anonymous session tokens

        who["current"] = UserIdentity(user_id="sub-0002", display_name="Mr. Some Name", auth_method="oidc")
        blocked = client.post(f"/api/v1/applications/{escalated_app_id}/checkout")
        assert blocked.status_code == 409 and "checked out by another session" in blocked.json()["detail"]
        assert "Ms. Some Name" not in blocked.text
        trail = client.get(f"/api/v1/applications/{escalated_app_id}/audit-trail").json()
        assert any(e["event_type"] == "CHECKOUT_ACQUIRED" and e["actor"] == "Ms. Some Name" for e in trail)
