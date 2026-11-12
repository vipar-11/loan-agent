"""
test_applicant_self_service.py
Loan Applicant self-service: sign-up (core + API), application ownership, "My Applications" status tracking, and
the decision SLA as applicants see it. Real accounts and sessions throughout — no dependency overrides.
"""
from datetime import datetime, timedelta
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from api.schemas import SlaInfo
from loan_agent.auth import AuthenticationError, LocalPasswordVerifier, Role, SignupError
from loan_agent.auth import users as user_directory
from loan_agent.config import AuthConfig
from tests.conftest import bearer_headers, create_user_with_password
from tests.test_auth import make_app
from tests.test_rbac import application_payload

CSRF = {"X-Requested-With": "ApexWorkbench"}
PASSWORD = "Correct-Horse-9"


# ===========================================================================
# Core: self-registration (loan_agent.auth.users.self_register)
# ===========================================================================
class TestSelfRegisterCore:
    def test_creates_an_active_applicant_with_the_chosen_password_ready_to_use(self, test_app_db):
        cfg = AuthConfig()
        user = user_directory.self_register(test_app_db, "Some.Name", PASSWORD, "  Mrs.   Some   Name  ", cfg)
        assert user["user_id"] == "some.name" and user["display_name"] == "Mrs. Some Name"
        assert user["role"] == "APPLICANT" and user["is_active"] is True
        assert user["must_change_password"] is False and user["password_hash"]
        # no empty-default-password step: the chosen password works immediately
        principal = LocalPasswordVerifier(test_app_db, cfg).verify("some.name", PASSWORD)
        assert principal.password_change_required is False and principal.role == Role.APPLICANT
        with pytest.raises(AuthenticationError):
            LocalPasswordVerifier(test_app_db, cfg).verify("some.name", "")  # no empty default to fall back to

    def test_blank_display_name_falls_back_to_the_user_id(self, test_app_db):
        user = user_directory.self_register(test_app_db, "applicant.one", PASSWORD, "", AuthConfig())
        assert user["display_name"] == "applicant.one"

    def test_cannot_register_as_another_role(self, test_app_db):
        """self_register has no role parameter at all: it is always APPLICANT."""
        import inspect

        assert "role" not in inspect.signature(user_directory.self_register).parameters

    def test_duplicate_user_id_is_refused(self, test_app_db):
        user_directory.self_register(test_app_db, "applicant.one", PASSWORD, "Mrs. Some Name", AuthConfig())
        with pytest.raises(SignupError, match="already taken"):
            user_directory.self_register(test_app_db, "APPLICANT.ONE", "Another-Pass-1", "Mrs. Some Other Name", AuthConfig())

    def test_cannot_take_over_an_administrator_provisioned_account(self, test_app_db):
        user_directory.create_user(test_app_db, "admin.one", Role.ADMINISTRATOR)  # empty default password, unclaimed
        with pytest.raises(SignupError, match="already taken"):
            user_directory.self_register(test_app_db, "admin.one", PASSWORD, "Impostor", AuthConfig())

    @pytest.mark.parametrize("bad_password", ["", "short", "applicant.one"])
    def test_password_policy_is_enforced(self, test_app_db, bad_password):
        with pytest.raises(SignupError):
            user_directory.self_register(test_app_db, "applicant.one", bad_password, "Mrs. Some Name", AuthConfig())

    def test_malformed_user_id_is_refused(self, test_app_db):
        with pytest.raises(SignupError):
            user_directory.self_register(test_app_db, "no spaces allowed", PASSWORD, "", AuthConfig())


# ===========================================================================
# HTTP: POST /api/v1/auth/signup
# ===========================================================================
@pytest.fixture
def app(test_app_db, test_checkpointer):
    application = make_app(test_app_db, test_checkpointer)
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def browser(app) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


def signup(client: TestClient, user_id: str, password: str = PASSWORD, display_name: str = ""):
    return client.post("/api/v1/auth/signup", json={"user_id": user_id, "password": password, "display_name": display_name})


class TestSignupEndpoint:
    def test_signs_up_and_signs_in_immediately(self, browser, test_app_db):
        res = signup(browser, "some.name", PASSWORD, "Mrs. Some Name")
        assert res.status_code == 201
        body = res.json()
        identity = body["identity"]
        assert identity["role"] == "APPLICANT" and identity["password_change_required"] is False
        assert identity["home_path"] == "/portal" and identity["pages"] == ["/portal", "/status"]
        assert "Welcome" in body["message"]
        assert "apex_session=" in res.headers["set-cookie"] and "HttpOnly" in res.headers["set-cookie"]

        # really signed in: no further action needed, and able to use the portal right away
        assert browser.get("/api/v1/session").json()["user_id"] == "some.name"
        assert browser.get("/portal").status_code == 200
        assert browser.get("/desk", follow_redirects=False).status_code == 404  # still just an applicant

    def test_cannot_grant_self_a_different_role(self, browser, test_app_db):
        """The payload has no role field; a client that sends one anyway is simply ignored."""
        res = browser.post("/api/v1/auth/signup",
                           json={"user_id": "applicant.one", "password": PASSWORD, "role": "ADMINISTRATOR"})
        assert res.status_code == 201
        assert test_app_db.get_user("applicant.one")["role"] == "APPLICANT"

    def test_duplicate_user_id(self, browser, test_app_db):
        create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT, PASSWORD)
        res = signup(browser, "applicant.one")
        assert res.status_code == 400 and "already taken" in res.json()["detail"]

    def test_weak_password(self, browser):
        res = signup(browser, "applicant.one", "short")
        assert res.status_code == 400

    def test_no_csrf_header_required_before_any_session_exists(self, browser):
        res = browser.post("/api/v1/auth/signup", json={"user_id": "applicant.one", "password": PASSWORD})
        assert res.status_code == 201  # unauthenticated request: the CSRF rule only guards existing sessions

    def test_signup_page_is_public_and_distinct_from_login(self, browser):
        page = browser.get("/signup")
        assert page.status_code == 200 and 'id="signup-form"' in page.text
        assert browser.get("/static/js/signup.js").status_code == 200
        assert browser.get("/login").text.count('href="/signup"') == 1


# ===========================================================================
# Ownership: "My Applications" (GET /api/v1/applications/mine[, /{id}])
# ===========================================================================
@pytest.fixture
def two_applicants(test_app_db):
    create_user_with_password(test_app_db, "applicant.one", Role.APPLICANT, PASSWORD, "Mrs. Some Applicant")
    create_user_with_password(test_app_db, "applicant.two", Role.APPLICANT, PASSWORD, "Mr. Some Applicant")
    create_user_with_password(test_app_db, "uw.one", Role.UNDERWRITER, PASSWORD, "Mr. Some Underwriter")
    create_user_with_password(test_app_db, "admin.one", Role.ADMINISTRATOR, PASSWORD, "Ms. Some Admin")
    return test_app_db


def submit_as(api_client: TestClient, user_id: str, key: str = "MRS_SOME_NAME") -> str:
    headers = bearer_headers(api_client, user_id, PASSWORD)
    res = api_client.post("/api/v1/applications", json=application_payload(key), headers=headers)
    assert res.status_code == 201, res.text
    return res.json()["application"]["application_id"]


class TestOwnershipIsolation:
    def test_applicant_sees_only_their_own_applications(self, app, two_applicants):
        api = TestClient(app)
        first_app = submit_as(api, "applicant.one", "MRS_SOME_NAME")
        second_app = submit_as(api, "applicant.two", "MR_SOME_NAME")

        first_h = bearer_headers(api, "applicant.one", PASSWORD)
        mine = api.get("/api/v1/applications/mine", headers=first_h).json()
        assert [a["application_id"] for a in mine] == [first_app]
        assert mine[0]["applicant_name"] == "Mrs. Some Name"

        second_h = bearer_headers(api, "applicant.two", PASSWORD)
        assert [a["application_id"] for a in api.get("/api/v1/applications/mine", headers=second_h).json()] == [second_app]

        # neither sees the other's file by ID — 404, not 403, like an unknown reference number; the message only
        # ever echoes the ID the caller supplied (same convention as every other application lookup in the API),
        # never "this belongs to someone else"
        cross = api.get(f"/api/v1/applications/mine/{first_app}", headers=second_h)
        assert cross.status_code == 404 and cross.json() == {"detail": f"Application '{first_app}' not found."}

    def test_underwriter_and_admin_see_the_real_owner_but_not_via_mine(self, app, two_applicants):
        api = TestClient(app)
        first_app = submit_as(api, "applicant.one")
        uw_h = bearer_headers(api, "uw.one", PASSWORD)
        listing = api.get("/api/v1/applications", headers=uw_h).json()
        assert next(a for a in listing if a["application_id"] == first_app)["submitted_by"] == "applicant.one"
        # underwriters / administrators have no personal "My Applications" — they do not submit loans
        assert api.get("/api/v1/applications/mine", headers=uw_h).status_code == 403
        admin_h = bearer_headers(api, "admin.one", PASSWORD)
        assert api.get("/api/v1/applications/mine", headers=admin_h).status_code == 403

    def test_applications_without_an_owner_belong_to_nobody(self, app, two_applicants, test_app_db):
        """Partner batch intake / pre-self-service submissions: no signed-in applicant, submitted_by is NULL."""
        from api.services import applications as app_service
        from api.schemas import ApplicationCreate

        app_service.create_application(
            test_app_db,
            ApplicationCreate(applicant_name="Legacy Borrower", ssn_last_4="9999", stated_annual_income=50000,
                              loan_amount_requested=5000, loan_purpose="Other", fcra_consent=True),
            actor="PARTNER:ACME", submitted_by=None,
        )
        api = TestClient(app)
        for user_id in ("applicant.one", "applicant.two"):
            h = bearer_headers(api, user_id, PASSWORD)
            assert api.get("/api/v1/applications/mine", headers=h).json() == []

    def test_mine_accepts_the_same_friendly_status_filters_as_the_full_queue(self, app, two_applicants):
        """status=processing (not "pending processing") must behave the same on /mine as on the full queue."""
        api = TestClient(app)
        app_id = submit_as(api, "applicant.one")
        test_db = two_applicants
        test_db.update_application(app_id, {"status": "PENDING_PROCESSING"}, actor="TEST")
        h = bearer_headers(api, "applicant.one", PASSWORD)

        for alias in ("processing", "pending-processing", "pending_processing"):
            res = api.get("/api/v1/applications/mine", params={"status": alias}, headers=h)
            assert [a["application_id"] for a in res.json()] == [app_id], alias
        assert api.get("/api/v1/applications/mine", params={"status": "approved"}, headers=h).json() == []
        assert [a["application_id"] for a in api.get("/api/v1/applications/mine", headers=h).json()] == [app_id]

    def test_my_application_status_excludes_internal_underwriting_detail(self, app, two_applicants):
        """Only a status/SLA view — no adjudicator memo, policy citations, checkout, or graph internals."""
        api = TestClient(app)
        submit_as(api, "applicant.one")
        h = bearer_headers(api, "applicant.one", PASSWORD)
        body = api.get("/api/v1/applications/mine", headers=h).json()[0]
        for leaked in ("agent_notes", "policy_citations", "checkout", "graph_state", "ssn_last_4", "ssn_masked",
                      "confidence_score", "officer"):
            assert leaked not in body, leaked
        assert set(body) == {
            "application_id", "applicant_name", "loan_amount_requested", "loan_purpose", "submitted_at",
            "updated_at", "status_label", "main_status", "decision_code", "decided_at", "adverse_action_reasons",
            "sla",
        }


# ===========================================================================
# SLA, end to end through the three personas (auto-approve, auto-deny, escalate + human decision)
# ===========================================================================
class TestSlaThroughTheLifecycle:
    def test_freshly_submitted_application_is_on_track(self, app, two_applicants):
        api = TestClient(app)
        app_id = submit_as(api, "applicant.one")
        h = bearer_headers(api, "applicant.one", PASSWORD)
        status = api.get(f"/api/v1/applications/mine/{app_id}", headers=h).json()
        assert status["status_label"] == "Received" and status["main_status"] == "new"
        assert status["sla"]["state"] == "ON_TRACK" and status["sla"]["business_days"] == 3
        assert status["sla"]["due_at"] and status["decided_at"] is None

    def test_auto_approval_is_decided_within_sla_with_no_adverse_action(self, app, two_applicants):
        api = TestClient(app)
        app_id = submit_as(api, "applicant.one", "MRS_SOME_NAME")
        admin_h = bearer_headers(api, "admin.one", PASSWORD)
        assert api.post(f"/api/v1/applications/{app_id}/workflow?sync=true", headers=admin_h).status_code == 202

        first_h = bearer_headers(api, "applicant.one", PASSWORD)
        status = api.get(f"/api/v1/applications/mine/{app_id}", headers=first_h).json()
        assert status["status_label"] == "Approved" and status["decision_code"] == "AUTO_APPROVED"
        assert status["sla"]["state"] == "DECIDED" and status["sla"]["decided_within_sla"] is True
        assert status["adverse_action_reasons"] is None

    def test_auto_denial_discloses_ecoa_adverse_action_reasons(self, app, two_applicants):
        api = TestClient(app)
        app_id = submit_as(api, "applicant.one", "MR_SOME_NAME")
        admin_h = bearer_headers(api, "admin.one", PASSWORD)
        api.post(f"/api/v1/applications/{app_id}/workflow?sync=true", headers=admin_h)

        first_h = bearer_headers(api, "applicant.one", PASSWORD)
        status = api.get(f"/api/v1/applications/mine/{app_id}", headers=first_h).json()
        assert status["status_label"] == "Denied" and status["decision_code"] == "AUTO_DENIED"
        assert status["adverse_action_reasons"]  # ECOA requires disclosing these to the applicant

    def test_escalated_application_shows_under_review_then_the_human_decision(self, app, two_applicants):
        api = TestClient(app)
        app_id = submit_as(api, "applicant.one", "MS_SOME_NAME")
        admin_h = bearer_headers(api, "admin.one", PASSWORD)
        api.post(f"/api/v1/applications/{app_id}/workflow?sync=true", headers=admin_h)

        first_h = bearer_headers(api, "applicant.one", PASSWORD)
        pending = api.get(f"/api/v1/applications/mine/{app_id}", headers=first_h).json()
        assert pending["status_label"] == "Under Review" and pending["sla"]["state"] in ("ON_TRACK", "DUE_SOON")

        uw_h = bearer_headers(api, "uw.one", PASSWORD)
        api.post(f"/api/v1/applications/{app_id}/checkout", headers=uw_h)
        decided = api.post(f"/api/v1/applications/{app_id}/decision", headers=uw_h,
                           json={"decision": "DENY", "notes": "Unable to verify income documentation."})
        assert decided.status_code == 200

        final = api.get(f"/api/v1/applications/mine/{app_id}", headers=first_h).json()
        assert final["status_label"] == "Denied" and final["decision_code"] == "HUMAN_DENIED"
        assert final["sla"]["state"] == "DECIDED" and final["decided_at"]

    def test_overdue_application_is_flagged_without_alarming_wording(self, app, two_applicants, test_app_db):
        api = TestClient(app)
        app_id = submit_as(api, "applicant.one")
        # submitted_at is immutable through the normal API; back-date it directly to simulate the passage of time.
        old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        test_app_db.conn.execute(
            "UPDATE loan_applications SET submitted_at = ? WHERE application_id = ?", (old, app_id)
        )
        test_app_db.conn.commit()

        first_h = bearer_headers(api, "applicant.one", PASSWORD)
        status = api.get(f"/api/v1/applications/mine/{app_id}", headers=first_h).json()
        assert status["sla"]["state"] == "OVERDUE"
        assert "patience" in status["sla"]["message"].lower()


def test_sla_info_schema_round_trips_the_engine_dataclass():
    """Guards the field-name contract between loan_agent.sla.SlaStatus and the API's SlaInfo."""
    from loan_agent import sla as sla_engine

    status = sla_engine.evaluate("2026-01-05 09:00:00", now=datetime(2026, 1, 5, 10, 0, 0))
    info = SlaInfo(**status.__dict__)
    assert info.state == "ON_TRACK"
