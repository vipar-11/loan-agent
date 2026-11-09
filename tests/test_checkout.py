"""
test_checkout.py
Review checkouts: a PENDING_REVIEW file can be open for review by only one identity at a time.
Identities are per-browser-tab sessions carried in the X-Apex-Session-Id header (see test_identity.py).
"""
import threading
import uuid
from datetime import datetime, timedelta

import pytest

from api.dependencies import SESSION_HEADER
from api.schemas import UserIdentity
from api.services import checkout as checkout_service
from loan_agent.db import ApplicationDatabase
from tests.test_api import client, escalated_app_id, persona_payload  # noqa: F401  (pytest fixtures)

NOTES = "Verified W-2 and pay stubs."


def new_tab(client) -> dict:
    """A fresh browser tab: establishes a session and returns the headers that tab sends on every call."""
    res = client.get("/api/v1/session")
    return {SESSION_HEADER: res.headers[SESSION_HEADER]}


def label(client, tab) -> str:
    return client.get("/api/v1/session", headers=tab).json()["display_name"]


def checkout(client, app_id, tab):
    return client.post(f"/api/v1/applications/{app_id}/checkout", headers=tab)


def renew(client, app_id, tab):
    return client.post(f"/api/v1/applications/{app_id}/checkout/renew", headers=tab)


def release(client, app_id, tab, **params):
    return client.delete(f"/api/v1/applications/{app_id}/checkout", headers=tab, params=params)


def decide(client, app_id, tab, decision="APPROVE", officer="Human Underwriter"):
    return client.post(
        f"/api/v1/applications/{app_id}/decision",
        headers=tab,
        json={"decision": decision, "officer": officer, "notes": NOTES},
    )


def events(client, app_id):
    return [e["event_type"] for e in client.get(f"/api/v1/applications/{app_id}/audit-trail").json()]


def ident(name: str) -> UserIdentity:
    return UserIdentity(user_id=str(uuid.uuid4()), display_name=name)


class TestLaunch:
    def test_desk_uses_the_tab_session_not_a_typed_name(self, client):
        desk = client.get("/desk").text
        assert 'id="session-label"' in desk and 'id="reviewer-name"' not in desk
        api_js = client.get("/static/js/api.js").text
        assert "sessionStorage" in api_js and "X-Apex-Session-Id" in api_js and "localStorage" not in api_js
        js = client.get("/static/js/desk.js").text
        assert "ApexAPI.checkout(" in js and "renewCheckout(" in js and "releaseCheckout(" in js

    def test_second_tab_cannot_open_a_file_checked_out_in_the_first(self, client, escalated_app_id):
        tab1, tab2 = new_tab(client), new_tab(client)
        first = checkout(client, escalated_app_id, tab1)
        assert first.status_code == 200
        body = first.json()
        assert body["held_by_you"] is True and body["state"] == "ACTIVE"
        assert body["checked_out_by"] == label(client, tab1)
        assert body["ttl_seconds"] == checkout_service.CHECKOUT_TTL_SECONDS
        assert body["heartbeat_seconds"] < body["ttl_seconds"]

        second = checkout(client, escalated_app_id, tab2)
        assert second.status_code == 409
        assert "checked out by another session" in second.json()["detail"]
        assert label(client, tab1) not in second.json()["detail"]

    def test_queue_and_detail_show_the_holder_relative_to_the_caller(self, client, escalated_app_id):
        tab1, tab2 = new_tab(client), new_tab(client)
        checkout(client, escalated_app_id, tab1)

        def row_for(tab):
            return next(a for a in client.get("/api/v1/applications?status=pending_review", headers=tab).json()
                        if a["application_id"] == escalated_app_id)

        mine = row_for(tab1)["checkout"]
        assert mine["held_by_you"] is True and mine["checked_out_by"] == label(client, tab1)
        other = row_for(tab2)["checkout"]
        assert other["held_by_you"] is False and other["checked_out_by"] == "another session"
        detail = client.get(f"/api/v1/applications/{escalated_app_id}", headers=tab2).json()
        assert detail["checkout"]["held_by_you"] is False

    def test_holder_is_never_disclosed_to_other_tabs(self, client, escalated_app_id):
        """Neither the holder's token nor its session label reaches another session."""
        tab1, tab2 = new_tab(client), new_tab(client)
        token1, label1 = tab1[SESSION_HEADER], label(client, tab1)
        checkout(client, escalated_app_id, tab1)
        for res in (
            client.get("/api/v1/applications", headers=tab2),
            client.get(f"/api/v1/applications/{escalated_app_id}", headers=tab2),
            checkout(client, escalated_app_id, tab2),
            renew(client, escalated_app_id, tab2),
            decide(client, escalated_app_id, tab2),
        ):
            assert token1 not in res.text and token1[:8].upper() not in res.text
            assert label1 not in res.text, res.text
        # the regulatory audit trail does record the real holder (never its token)
        trail = client.get(f"/api/v1/applications/{escalated_app_id}/audit-trail", headers=tab2).text
        assert label1 in trail and token1 not in trail

    def test_same_tab_can_reopen_without_a_new_audit_event(self, client, escalated_app_id):
        tab = new_tab(client)
        first = checkout(client, escalated_app_id, tab).json()
        again = checkout(client, escalated_app_id, tab)  # e.g. page reload in the same tab
        assert again.status_code == 200 and again.json()["checked_out_at"] == first["checked_out_at"]
        assert events(client, escalated_app_id).count("CHECKOUT_ACQUIRED") == 1

    def test_only_pending_review_files_can_be_checked_out(self, client):
        tab = new_tab(client)
        new_id = client.post("/api/v1/applications", json=persona_payload("MS_SOME_NAME")).json()["application"]["application_id"]
        res = checkout(client, new_id, tab)
        assert res.status_code == 409 and "not awaiting human review" in res.json()["detail"]
        assert checkout(client, "APP-NOPE", tab).status_code == 404

    def test_case_insensitive_application_id(self, client, escalated_app_id):
        tab1, tab2 = new_tab(client), new_tab(client)
        assert checkout(client, escalated_app_id.lower(), tab1).status_code == 200
        assert checkout(client, escalated_app_id, tab2).status_code == 409

    def test_headerless_checkout_is_held_by_the_session_issued_on_that_response(self, client, escalated_app_id):
        """The router-level and endpoint-level identity dependencies must resolve to ONE identity per request."""
        res = client.post(f"/api/v1/applications/{escalated_app_id}/checkout")
        assert res.status_code == 200 and res.json()["held_by_you"] is True
        issued = {SESSION_HEADER: res.headers[SESSION_HEADER]}
        assert renew(client, escalated_app_id, issued).status_code == 200


class TestRelease:
    def test_closing_the_review_frees_the_file(self, client, escalated_app_id):
        tab1, tab2 = new_tab(client), new_tab(client)
        checkout(client, escalated_app_id, tab1)
        res = release(client, escalated_app_id, tab1)
        assert res.status_code == 200 and res.json()["released"] is True
        assert checkout(client, escalated_app_id, tab2).status_code == 200
        assert "CHECKOUT_RELEASED" in events(client, escalated_app_id)

    def test_other_tab_cannot_release(self, client, escalated_app_id):
        tab1, tab2 = new_tab(client), new_tab(client)
        checkout(client, escalated_app_id, tab1)
        res = release(client, escalated_app_id, tab2)
        assert res.status_code == 200 and res.json()["released"] is False
        assert checkout(client, escalated_app_id, tab2).status_code == 409

    def test_release_is_idempotent(self, client, escalated_app_id):
        tab = new_tab(client)
        assert release(client, escalated_app_id, tab).json()["released"] is False
        assert release(client, "APP-NOPE", tab).status_code == 404

    def test_renew_via_api(self, client, escalated_app_id):
        tab1, tab2 = new_tab(client), new_tab(client)
        checkout(client, escalated_app_id, tab1)
        res = renew(client, escalated_app_id, tab1)
        assert res.status_code == 200 and res.json()["held_by_you"] is True
        assert renew(client, escalated_app_id, tab2).status_code == 409
        assert renew(client, "APP-NOPE", tab1).status_code == 404

    def test_administrator_force_release_is_audited(self, client, escalated_app_id):
        tab1, admin_tab, tab2 = new_tab(client), new_tab(client), new_tab(client)
        checkout(client, escalated_app_id, tab1)
        res = release(client, escalated_app_id, admin_tab, force="true", actor="Ops Admin")
        assert res.json()["released"] is True
        trail = client.get(f"/api/v1/applications/{escalated_app_id}/audit-trail").json()
        forced = [e for e in trail if e["event_type"] == "CHECKOUT_FORCE_RELEASED"]
        assert forced and forced[0]["actor"] == "Ops Admin" and label(client, tab1) in forced[0]["details"]
        assert checkout(client, escalated_app_id, tab2).status_code == 200

    def test_force_release_is_final_for_the_open_review(self, client, escalated_app_id):
        """The holder's heartbeat must not silently re-acquire a file an administrator released."""
        tab1, admin_tab = new_tab(client), new_tab(client)
        checkout(client, escalated_app_id, tab1)
        release(client, escalated_app_id, admin_tab, force="true", actor="Ops Admin")
        res = renew(client, escalated_app_id, tab1)
        assert res.status_code == 409 and "no longer active" in res.json()["detail"]
        assert client.get(f"/api/v1/applications/{escalated_app_id}").json()["checkout"] is None


class TestLeaseExpiry:
    def test_abandoned_checkout_lapses_and_can_be_taken_over(self, client, escalated_app_id, test_app_db):
        tab1, tab2 = ident("Session AAAA0001"), ident("Session BBBB0002")
        t0 = datetime(2026, 1, 5, 9, 0, 0)
        checkout_service.checkout(test_app_db, escalated_app_id, tab1, now=t0)

        before_expiry = t0 + timedelta(seconds=checkout_service.CHECKOUT_TTL_SECONDS - 1)
        with pytest.raises(checkout_service.CheckoutConflict):
            checkout_service.checkout(test_app_db, escalated_app_id, tab2, now=before_expiry)

        after_expiry = t0 + timedelta(seconds=checkout_service.CHECKOUT_TTL_SECONDS + 1)
        info = checkout_service.checkout(test_app_db, escalated_app_id, tab2, now=after_expiry)
        assert info.checked_out_by == "Session BBBB0002" and info.held_by_you
        details = [e["details"] for e in test_app_db.get_audit_trail(escalated_app_id) if e["event_type"] == "CHECKOUT_ACQUIRED"]
        assert "Previous checkout by Session AAAA0001 had expired" in details[-1]

    def test_heartbeat_extends_the_lease(self, client, escalated_app_id, test_app_db):
        tab1, tab2 = ident("Session AAAA0001"), ident("Session BBBB0002")
        t0 = datetime(2026, 1, 5, 9, 0, 0)
        ttl = checkout_service.CHECKOUT_TTL_SECONDS
        checkout_service.checkout(test_app_db, escalated_app_id, tab1, now=t0)
        checkout_service.renew(test_app_db, escalated_app_id, tab1, now=t0 + timedelta(seconds=ttl - 10))
        with pytest.raises(checkout_service.CheckoutConflict):  # past the original lease, within the renewed one
            checkout_service.checkout(test_app_db, escalated_app_id, tab2, now=t0 + timedelta(seconds=ttl + 60))

    def test_lapsed_but_untouched_checkout_can_still_be_renewed(self, client, escalated_app_id, test_app_db):
        """Laptop asleep past the lease, nobody else opened the file meanwhile: the review simply carries on."""
        tab1 = ident("Session AAAA0001")
        t0 = datetime(2026, 1, 5, 9, 0, 0)
        checkout_service.checkout(test_app_db, escalated_app_id, tab1, now=t0)
        assert checkout_service.renew(test_app_db, escalated_app_id, tab1, now=t0 + timedelta(hours=1)).held_by_you

    def test_lapsed_checkout_taken_over_cannot_be_renewed(self, client, escalated_app_id, test_app_db):
        tab1, tab2 = ident("Session AAAA0001"), ident("Session BBBB0002")
        t0 = datetime(2026, 1, 5, 9, 0, 0)
        checkout_service.checkout(test_app_db, escalated_app_id, tab1, now=t0)
        later = t0 + timedelta(seconds=checkout_service.CHECKOUT_TTL_SECONDS + 1)
        checkout_service.checkout(test_app_db, escalated_app_id, tab2, now=later)
        with pytest.raises(checkout_service.CheckoutConflict, match="now checked out by another session"):
            checkout_service.renew(test_app_db, escalated_app_id, tab1, now=later)

    def test_expired_checkout_is_not_shown_in_the_queue(self, client, escalated_app_id, test_app_db):
        checkout_service.checkout(test_app_db, escalated_app_id, ident("Session AAAA0001"), now=datetime(2000, 1, 1))
        assert client.get(f"/api/v1/applications/{escalated_app_id}").json()["checkout"] is None


class TestDecisionEnforcement:
    def test_other_tab_cannot_decide_a_checked_out_file(self, client, escalated_app_id, test_app_db):
        tab1, tab2 = new_tab(client), new_tab(client)
        checkout(client, escalated_app_id, tab1)
        res = decide(client, escalated_app_id, tab2)
        assert res.status_code == 409 and "checked out by another session" in res.json()["detail"]
        assert test_app_db.get_application(escalated_app_id)["status"] == "PENDING_REVIEW"

    def test_typed_officer_name_does_not_grant_access(self, client, escalated_app_id):
        """Exclusivity is keyed on the session, not on the free-text officer name."""
        tab1, tab2 = new_tab(client), new_tab(client)
        checkout(client, escalated_app_id, tab1)
        assert decide(client, escalated_app_id, tab2, officer=label(client, tab1)).status_code == 409

    def test_holder_decides_and_checkout_is_released(self, client, escalated_app_id, test_app_db):
        tab = new_tab(client)
        checkout(client, escalated_app_id, tab)
        res = decide(client, escalated_app_id, tab, "DENY", officer="Ms. Some Name, Credit Committee")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "HUMAN_DENIED" and body["adjudication"]["officer"] == "Ms. Some Name, Credit Committee"
        assert body["checkout"] is None and test_app_db.get_checkout(escalated_app_id) is None

    def test_decision_without_prior_checkout_takes_one_implicitly(self, client, escalated_app_id, test_app_db):
        """API clients that never opened the file can still decide — as long as nobody else holds it."""
        assert decide(client, escalated_app_id, new_tab(client)).status_code == 200
        assert test_app_db.get_checkout(escalated_app_id) is None

    def test_failed_determination_hands_the_file_back_to_the_holder(self, client, escalated_app_id, test_app_db, monkeypatch):
        from api.services import workflows as workflow_service

        def boom(**kwargs):
            raise ValueError("simulated failure")

        monkeypatch.setattr(workflow_service, "resume_with_human_decision", boom)
        tab1, tab2 = new_tab(client), new_tab(client)
        checkout(client, escalated_app_id, tab1)
        assert decide(client, escalated_app_id, tab1).status_code == 409
        row = test_app_db.get_checkout(escalated_app_id)
        assert row["holder_key"] == tab1[SESSION_HEADER] and row["state"] == "ACTIVE"
        assert checkout(client, escalated_app_id, tab2).status_code == 409

    def test_failed_implicit_determination_leaves_no_checkout(self, client, escalated_app_id, test_app_db, monkeypatch):
        from api.services import workflows as workflow_service

        def boom(**kwargs):
            raise ValueError("simulated failure")

        monkeypatch.setattr(workflow_service, "resume_with_human_decision", boom)
        assert decide(client, escalated_app_id, new_tab(client)).status_code == 409
        assert test_app_db.get_checkout(escalated_app_id) is None


class TestDecisionClaim:
    NOW, LATER = "2026-01-05 09:00:00", "2026-01-05 09:05:00"

    def test_claim_is_single_use(self, test_app_db):
        """Two requests from the same tab at once (double-click): only one may record the decision."""
        test_app_db.acquire_checkout("APP-X", "tok-1", "Session 1", self.NOW, self.LATER)
        assert test_app_db.claim_checkout_for_decision("APP-X", "tok-1", self.NOW, self.LATER) is True
        assert test_app_db.claim_checkout_for_decision("APP-X", "tok-1", self.NOW, self.LATER) is False
        # while a determination is in flight, nobody (not even the holder) can re-acquire
        assert test_app_db.acquire_checkout("APP-X", "tok-1", "Session 1", self.NOW, self.LATER)["state"] == "DECIDING"
        assert test_app_db.restore_checkout("APP-X", "tok-1") is True
        assert test_app_db.claim_checkout_for_decision("APP-X", "tok-1", self.NOW, self.LATER) is True

    def test_claim_requires_an_unexpired_checkout_held_by_the_caller(self, test_app_db):
        test_app_db.acquire_checkout("APP-X", "tok-1", "Session 1", self.NOW, self.LATER)
        assert test_app_db.claim_checkout_for_decision("APP-X", "tok-2", "2026-01-05 09:01:00", "2026-01-05 09:06:00") is False
        assert test_app_db.claim_checkout_for_decision("APP-X", "tok-1", "2026-01-05 09:06:00", "2026-01-05 09:11:00") is False


class TestCrossConnectionExclusivity:
    def test_exactly_one_holder_wins_across_independent_connections(self, tmp_path):
        """
        Two ApplicationDatabase instances on the same file stand in for two API worker processes:
        their in-process locks are independent, so only the SQL statement itself can guarantee exclusivity.
        """
        path = tmp_path / "shared.db"
        db_a, db_b = ApplicationDatabase(db_path=path), ApplicationDatabase(db_path=path)
        db_a.conn.execute("PRAGMA busy_timeout = 5000")
        db_b.conn.execute("PRAGMA busy_timeout = 5000")
        try:
            now_s, exp_s = "2026-01-05 09:00:00", "2026-01-05 09:05:00"
            for i in range(40):
                app_id = f"APP-RACE-{i:03d}"
                barrier, won = threading.Barrier(2), {}

                def grab(db, token):
                    barrier.wait()
                    row = db.acquire_checkout(app_id, token, f"Session {token}", now_s, exp_s)
                    won[token] = row["holder_key"] == token

                threads = [threading.Thread(target=grab, args=(db_a, "tab-a")),
                           threading.Thread(target=grab, args=(db_b, "tab-b"))]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)
                assert sorted(won.values()) == [False, True], f"round {i}: {won}"
        finally:
            db_a.close()
            db_b.close()
