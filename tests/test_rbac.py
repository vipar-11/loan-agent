"""
test_rbac.py
Role-based access control, end-to-end with real accounts and bearer tokens (no dependency overrides).

    Loan Applicant     Applicant Portal: submit applications.
    Loan Underwriter   Underwriter Desk: queue/archive, review dossier, checkout, determinations.
    Administrator      Administrator console, workflow runs, checkout overrides, read-only application access —
                       but no credit authority (segregation of duties).

The ACCESS matrix must list every API route: a new endpoint fails test_matrix_covers_every_route until its access
has been decided deliberately.
"""
from typing import Dict, Generator

import pytest
from fastapi.testclient import TestClient

from api.dependencies import get_checkpointer, get_db
from api.main import create_app
from loan_agent.auth import Role
from loan_agent.mock_apis import APPLICANT_PROFILES
from tests.conftest import bearer_headers, create_user_with_password
from tests.test_auth import PUBLIC_ROUTES

A, U, D = Role.APPLICANT, Role.UNDERWRITER, Role.ADMINISTRATOR
ALL = {A, U, D}
USERS = {A: ("jane.applicant", "Jane Applicant"), U: ("uw.smith", "Sam Smith"), D: ("ops.lee", "Lee Ops")}


def application_payload(key: str = "MS_SOME_NAME") -> dict:
    p = APPLICANT_PROFILES[key]
    return {k: p[k] for k in ("applicant_name", "ssn_last_4", "stated_annual_income", "loan_amount_requested",
                              "loan_purpose")} | {"fcra_consent": True}


# (method, OpenAPI path, concrete URL, JSON body, roles allowed). Unknown IDs keep allowed calls side-effect free.
ACCESS = [
    ("GET", "/api/v1/session", "/api/v1/session", None, ALL),
    ("POST", "/api/v1/auth/password", "/api/v1/auth/password", {"new_password": "x"}, ALL),
    ("GET", "/api/v1/personas", "/api/v1/personas", None, ALL),
    ("GET", "/api/v1/calculator/installment", "/api/v1/calculator/installment?loan_amount=15000", None, ALL),
    ("GET", "/api/v1/policy", "/api/v1/policy", None, ALL),
    ("GET", "/api/v1/system", "/api/v1/system", None, {D}),
    ("POST", "/api/v1/applications", "/api/v1/applications", application_payload(), {A}),
    ("GET", "/api/v1/applications/mine", "/api/v1/applications/mine", None, {A}),
    ("GET", "/api/v1/applications/mine/{application_id}", "/api/v1/applications/mine/APP-X", None, {A}),
    ("GET", "/api/v1/applications", "/api/v1/applications", None, {U, D}),
    ("GET", "/api/v1/applications/{application_id}", "/api/v1/applications/APP-X", None, {U, D}),
    ("GET", "/api/v1/applications/{application_id}/audit-trail", "/api/v1/applications/APP-X/audit-trail", None, {U, D}),
    ("GET", "/api/v1/applications/{application_id}/dossier", "/api/v1/applications/APP-X/dossier", None, {U}),
    ("GET", "/api/v1/applications/{application_id}/classifications/heuristic",
     "/api/v1/applications/APP-X/classifications/heuristic", None, {U}),
    ("POST", "/api/v1/applications/{application_id}/ratios/recalculate",
     "/api/v1/applications/APP-X/ratios/recalculate", {"classifications": {}}, {U}),
    ("POST", "/api/v1/applications/{application_id}/workflow", "/api/v1/applications/APP-X/workflow", None, {D}),
    ("POST", "/api/v1/applications/next-for-review", "/api/v1/applications/next-for-review", None, {U}),
    ("POST", "/api/v1/applications/{application_id}/checkout", "/api/v1/applications/APP-X/checkout", None, {U}),
    ("POST", "/api/v1/applications/{application_id}/checkout/renew",
     "/api/v1/applications/APP-X/checkout/renew", None, {U}),
    ("DELETE", "/api/v1/applications/{application_id}/checkout", "/api/v1/applications/APP-X/checkout", None, {U, D}),
    ("POST", "/api/v1/applications/{application_id}/decision", "/api/v1/applications/APP-X/decision",
     {"decision": "APPROVE", "notes": "n"}, {U}),
    ("POST", "/api/v1/workflows/run", "/api/v1/workflows/run", {"dry_run": True}, {D}),
    ("GET", "/api/v1/workflows/runs/{run_id}", "/api/v1/workflows/runs/999", None, {D}),
    ("GET", "/api/v1/stats", "/api/v1/stats", None, {U, D}),
    ("GET", "/api/v1/admin/status-summary", "/api/v1/admin/status-summary", None, {D}),
    ("GET", "/api/v1/admin/scheduler", "/api/v1/admin/scheduler", None, {D}),
    ("GET", "/api/v1/admin/scheduler/config", "/api/v1/admin/scheduler/config", None, {D}),
    ("PUT", "/api/v1/admin/scheduler/config", "/api/v1/admin/scheduler/config", {}, {D}),
    ("GET", "/api/v1/admin/workflow-runs", "/api/v1/admin/workflow-runs", None, {D}),
    ("GET", "/api/v1/admin/intake", "/api/v1/admin/intake", None, {D}),
    ("GET", "/api/v1/admin/intake/batches", "/api/v1/admin/intake/batches", None, {D}),
    ("GET", "/api/v1/admin/intake/batches/{batch_id}", "/api/v1/admin/intake/batches/999", None, {D}),
    ("POST", "/api/v1/admin/intake/scan", "/api/v1/admin/intake/scan", None, {D}),
]


@pytest.fixture
def api(test_app_db, test_checkpointer) -> Generator[TestClient, None, None]:
    app = create_app(scheduler_enabled=False, intake_enabled=False)
    app.dependency_overrides[get_db] = lambda: test_app_db
    app.dependency_overrides[get_checkpointer] = lambda: test_checkpointer
    for role, (user_id, name) in USERS.items():
        create_user_with_password(test_app_db, user_id, role, name=name)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def as_role(api) -> Dict[Role, dict]:
    return {role: bearer_headers(api, user_id) for role, (user_id, _name) in USERS.items()}


def call(api, method, url, body, headers):
    return api.request(method, url, json=body, headers=headers)


class TestApiAccessMatrix:
    def test_matrix_covers_every_route(self, api):
        schema = api.get("/openapi.json").json()
        routes = {(m.upper(), p) for p, ops in schema["paths"].items() for m in ops}
        covered = {(m, p) for m, p, *_ in ACCESS} | PUBLIC_ROUTES
        assert routes == covered, f"missing: {routes - covered}; stale: {covered - routes}"

    @pytest.mark.parametrize("method,path,url,body,allowed", ACCESS, ids=[f"{m} {p}" for m, p, *_ in ACCESS])
    def test_each_role(self, api, as_role, method, path, url, body, allowed):
        for role in Role:
            res = call(api, method, url, body, as_role[role])
            if role in allowed:
                assert res.status_code not in (401, 403), (role, res.status_code, res.text)
            else:
                assert res.status_code == 403, (role, res.status_code, res.text)
                # says nothing about the caller's role or the access the endpoint needs
                assert res.json() == {"detail": "You do not have access to this resource."}

    def test_only_administrators_can_force_release(self, api, as_role):
        url = "/api/v1/applications/APP-X/checkout?force=true"
        assert call(api, "DELETE", url, None, as_role[U]).status_code == 403
        assert call(api, "DELETE", url, None, as_role[D]).status_code == 404  # allowed; no such application


class TestPageAccess:
    HOME = {A: "/portal", U: "/desk", D: "/admin"}
    PAGES = {A: ["/portal", "/status"], U: ["/desk"], D: ["/admin"]}
    ALL_PAGES = ("/portal", "/status", "/desk", "/admin")

    @pytest.fixture
    def browsers(self, api) -> Dict[Role, TestClient]:
        clients = {}
        for role, (user_id, _name) in USERS.items():
            c = TestClient(api.app)
            assert c.post("/api/v1/auth/login", json={"user_id": user_id, "password": "Correct-Horse-9"}).status_code == 200
            clients[role] = c
        return clients

    def test_each_role_opens_only_its_permitted_pages(self, browsers):
        for role, browser in browsers.items():
            for page in self.ALL_PAGES:
                expected = 200 if page in self.PAGES[role] else 404
                assert browser.get(page, follow_redirects=False).status_code == expected, (role, page)

    def test_forbidden_pages_are_indistinguishable_from_missing_pages(self, browsers):
        """Same status, same headers, same bytes: nobody can tell which other pages exist."""
        for role, browser in browsers.items():
            missing = browser.get("/non-existent-page")
            assert missing.status_code == 404
            for probe in [p for p in self.ALL_PAGES if p not in self.PAGES[role]] + [
                "/no/such/thing", "/admin-console", "/desk/"
            ]:
                res = browser.get(probe, follow_redirects=False)
                assert res.status_code == 404, (role, probe)
                assert res.content == missing.content, (role, probe)
                assert {k: v for k, v in res.headers.items() if k != "date"} == \
                    {k: v for k, v in missing.headers.items() if k != "date"}, (role, probe)

    def test_not_found_page_reveals_no_role_or_access_information(self, browsers):
        for role, browser in browsers.items():
            html = browser.get("/non-existent-page").text
            assert "Page not found" in html
            assert 'id="session-role"' not in html
            for leak in ("Loan Applicant", "Loan Underwriter", "access denied", "permission", "not available",
                         "your role"):
                assert leak.lower() not in html.lower(), (role, leak)

    def test_signed_out_visitors_get_the_same_answer_for_real_and_missing_pages(self, api):
        anonymous = TestClient(api.app)
        for page in ("/admin", "/desk", "/portal", "/non-existent-page", "/admin-console"):
            res = anonymous.get(page, follow_redirects=False)
            assert res.status_code == 303 and res.headers["location"] == f"/login?next={page}", page
            assert res.content == b""

    def test_api_and_static_paths_keep_plain_404s(self, browsers):
        browser = browsers[A]
        assert browser.get("/api/v1/no-such-endpoint").json() == {"detail": "Not Found"}
        assert browser.get("/static/js/no-such-file.js").status_code == 404
        assert browser.get("/static/js/api.js").status_code == 200  # the catch-all does not shadow static files

    def test_navigation_lists_only_permitted_pages(self, browsers):
        for role, browser in browsers.items():
            html = browser.get(self.HOME[role]).text
            for page in self.ALL_PAGES:
                present = (f'class="nav-link active" href="{page}"' in html or f'class="nav-link " href="{page}"' in html)
                assert present == (page in self.PAGES[role]), (role, page)

    def test_root_lands_on_the_role_home_page(self, browsers):
        for role, browser in browsers.items():
            assert browser.get("/", follow_redirects=False).headers["location"] == self.HOME[role]

    def test_identity_lists_permitted_pages(self, browsers):
        for role, browser in browsers.items():
            me = browser.get("/api/v1/session").json()
            assert me["pages"] == self.PAGES[role] and me["home_path"] == self.HOME[role]


class TestThreePersonaJourney:
    def test_submit_run_review_decide(self, api, as_role, test_app_db):
        # Loan Applicant submits; they cannot see the queue
        res = api.post("/api/v1/applications", json=application_payload("MS_SOME_NAME"), headers=as_role[A])
        assert res.status_code == 201
        app_id = res.json()["application"]["application_id"]
        assert api.get("/api/v1/applications", headers=as_role[A]).status_code == 403

        # Administrator runs the underwriting workflow (escalates to human review) — but may not decide
        ran = api.post(f"/api/v1/applications/{app_id}/workflow?sync=true", headers=as_role[D])
        assert ran.status_code == 202 and ran.json()["status"] == "PENDING_REVIEW"
        assert api.post(f"/api/v1/applications/{app_id}/decision", headers=as_role[D],
                        json={"decision": "APPROVE", "notes": "n"}).status_code == 403

        # Loan Underwriter reviews and decides; the officer is the signed-in user
        assert api.post(f"/api/v1/applications/{app_id}/checkout", headers=as_role[U]).status_code == 200
        assert api.get(f"/api/v1/applications/{app_id}/dossier", headers=as_role[U]).status_code == 200
        decided = api.post(f"/api/v1/applications/{app_id}/decision", headers=as_role[U],
                           json={"decision": "APPROVE", "notes": "Verified W-2 and pay stubs."})
        assert decided.status_code == 200
        assert decided.json()["adjudication"]["officer"] == "Sam Smith"
        assert decided.json()["adjudication"]["officer_user_id"] == "uw.smith"

        # every step is attributed to the real, signed-in user
        actors = {e["event_type"]: e["actor"] for e in test_app_db.get_audit_trail(app_id)}
        assert actors["APPLICATION_SUBMITTED"] == "Jane Applicant (jane.applicant)"
        assert actors["WORKFLOW_INITIATED"] == "Lee Ops (ops.lee)"
        assert actors["CHECKOUT_ACQUIRED"] == "Sam Smith (uw.smith)"
        assert actors["HUMAN_REVIEW_DECISION"] == "Sam Smith (uw.smith)"

    def test_underwriters_exclude_each_other(self, api, as_role, test_app_db):
        create_user_with_password(test_app_db, "uw.other", Role.UNDERWRITER, name="Other Underwriter")
        other = bearer_headers(api, "uw.other")
        app_id = api.post("/api/v1/applications", json=application_payload(), headers=as_role[A]).json()["application"]["application_id"]
        api.post(f"/api/v1/applications/{app_id}/workflow?sync=true", headers=as_role[D])
        assert api.post(f"/api/v1/applications/{app_id}/checkout", headers=as_role[U]).status_code == 200
        blocked = api.post(f"/api/v1/applications/{app_id}/checkout", headers=other)
        assert blocked.status_code == 409 and "Sam Smith" not in blocked.text
        decided = api.post(f"/api/v1/applications/{app_id}/decision", headers=other,
                           json={"decision": "DENY", "notes": "n"})
        assert decided.status_code == 409
        # the administrator can free the file for the other underwriter
        assert api.delete(f"/api/v1/applications/{app_id}/checkout?force=true", headers=as_role[D]).json()["released"]
        assert api.post(f"/api/v1/applications/{app_id}/checkout", headers=other).status_code == 200
