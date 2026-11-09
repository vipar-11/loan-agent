"""
test_batch_intake.py
Partner batch intake: CSV batch files delivered over FTP by approved partners.
"""
import csv
import os
import shutil
import time
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from api.dependencies import get_checkpointer, get_db
from api.main import create_app
from api.services import batch_intake as intake
from api.services import workflows as workflow_service
from loan_agent.config import IntakeConfig, load_intake_config

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "partner_kit" / "loan_batch_sample.csv"
ERRORS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "loan_batch_with_errors.csv"
HEADER = ",".join(intake.COLUMNS)
PARTNER = "ACME_BROKERS"


@pytest.fixture
def cfg(tmp_path) -> IntakeConfig:
    return IntakeConfig(
        enabled=True, root_dir=tmp_path / "intake", poll_seconds=0.2, settle_seconds=0.0,
        max_file_bytes=64 * 1024, max_rows=50,
        approved_partners={PARTNER: "Acme Loan Brokers LLC", "SUMMIT_FINANCIAL": "Summit Financial Partners Inc."},
    )


def drop(cfg: IntakeConfig, name: str, content: str | bytes, partner: str = PARTNER) -> Path:
    folder = cfg.inbox_dir / partner if partner else cfg.inbox_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def row(ref="R-1", name="Ms. Some Name", ssn="1111", income="96000.00", amount="15000.00",
        purpose="Home Improvement", consent="Y") -> str:
    return ",".join([ref, name, ssn, income, amount, purpose, consent])


def results_of(batch) -> list:
    with open(batch["results_path"], encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------
class TestRowValidation:
    def _errors(self, **fields):
        rec = dict(zip(intake.COLUMNS, row().split(",")))
        rec.update(fields)
        return intake.validate_record(rec, 1).errors

    def test_valid_row(self):
        parsed = intake.validate_record(dict(zip(intake.COLUMNS, row().split(","))), 1)
        assert parsed.errors == [] and parsed.application.loan_purpose == "Home Improvement"
        assert parsed.application.fcra_consent is True and parsed.application.ssn_last_4 == "1111"

    @pytest.mark.parametrize("field,value,fragment", [
        ("partner_reference", "", "partner_reference: required"),
        ("partner_reference", "has space", "partner_reference: must be 1-64"),
        ("partner_reference", "x" * 65, "partner_reference: must be 1-64"),
        ("applicant_name", "   ", "applicant_name: required"),
        ("applicant_name", "N" * 121, "applicant_name"),
        ("ssn_last_4", "123", "ssn_last_4: must be exactly 4 digits"),
        ("ssn_last_4", "12a4", "ssn_last_4: must be exactly 4 digits"),
        ("stated_annual_income", "$85,000", "stated_annual_income: '$85,000' is not a plain decimal"),
        ("stated_annual_income", "0", "stated_annual_income: 0.00 is out of range"),
        ("stated_annual_income", "85000.123", "not a plain decimal"),
        ("loan_amount_requested", "999.99", "loan_amount_requested: 999.99 is out of range"),
        ("loan_amount_requested", "50000.01", "loan_amount_requested: 50,000.01 is out of range"),
        ("loan_purpose", "Vacation", "loan_purpose: 'Vacation' is not one of"),
        ("fcra_consent", "yes", "fcra_consent: 'yes' must be Y or N"),
        ("fcra_consent", "", "fcra_consent: required"),
    ])
    def test_invalid_fields_are_flagged(self, field, value, fragment):
        errors = self._errors(**{field: value})
        assert any(fragment in e for e in errors), errors

    def test_boundaries_and_normalisation(self):
        assert self._errors(loan_amount_requested="1000") == []
        assert self._errors(loan_amount_requested="50000.00") == []
        assert self._errors(ssn_last_4="0420") == []
        parsed = intake.validate_record(dict(zip(intake.COLUMNS, row(purpose="debt CONSOLIDATION", consent="n").split(","))), 1)
        assert parsed.application.loan_purpose == "Debt Consolidation" and parsed.application.fcra_consent is False

    def test_every_problem_is_reported_at_once(self):
        errors = self._errors(ssn_last_4="12345", stated_annual_income="-5", loan_amount_requested="abc",
                              loan_purpose="Travel", fcra_consent="X")
        assert len(errors) == 5


# ---------------------------------------------------------------------------
# File-level parsing
# ---------------------------------------------------------------------------
class TestFileParsing:
    def test_header_any_order_and_case_bom_crlf(self):
        cols = list(reversed(intake.COLUMNS))
        values = dict(zip(intake.COLUMNS, row().split(",")))
        content = ("\ufeff" + ",".join(c.upper() for c in cols) + "\r\n" + ",".join(values[c] for c in cols) + "\r\n\r\n")
        rows = intake.parse_batch(content.encode("utf-8"), 10)
        assert len(rows) == 1 and rows[0].errors == []

    @pytest.mark.parametrize("content,fragment", [
        (b"", "File is empty"),
        (HEADER.encode(), "no application rows"),
        (b"partner_reference,applicant_name\nR1,Some Name\n", "missing column(s): ssn_last_4"),
        ((HEADER + ",notes\n" + row() + ",x\n").encode(), "unknown column(s): 'notes'"),
        ((HEADER + ",fcra_consent\n").encode(), "duplicate column(s): fcra_consent"),
        ("partner_reference\n\xe9".encode("latin-1"), "not valid UTF-8"),
        (b"a,b\x00c\n", "NUL bytes"),
    ])
    def test_file_level_rejections(self, content, fragment):
        with pytest.raises(intake.FileRejected, match=None) as exc:
            intake.parse_batch(content, 10)
        assert fragment in str(exc.value)

    def test_row_limit(self):
        content = HEADER + "\n" + "\n".join(row(ref=f"R-{i}") for i in range(11))
        with pytest.raises(intake.FileRejected, match="more than the maximum of 10"):
            intake.parse_batch(content.encode(), 10)

    def test_in_file_duplicate_references_flag_every_occurrence(self):
        content = "\n".join([HEADER, row(ref="DUP"), row(ref="OK"), row(ref="DUP")])
        rows = intake.parse_batch(content.encode(), 10)
        assert [bool(r.errors) for r in rows] == [True, False, True]
        assert "rows 1, 3" in rows[0].errors[0]

    def test_wrong_field_count_is_a_row_problem(self):
        rows = intake.parse_batch("\n".join([HEADER, row() + ",EXTRA", row(ref="R-2")]).encode(), 10)
        assert "has 8 field(s) but the header has 7" in rows[0].errors[0] and rows[1].errors == []


# ---------------------------------------------------------------------------
# End-to-end processing of dropped files
# ---------------------------------------------------------------------------
class TestProcessing:
    def test_published_sample_is_fully_accepted(self, cfg, test_app_db):
        """Guards the sample we publish to partners against drifting out of spec."""
        drop(cfg, SAMPLE.name, SAMPLE.read_bytes())
        [batch] = intake.scan_inbox(test_app_db, cfg)
        lines = [l for l in SAMPLE.read_text().splitlines()[1:] if l.strip()]
        assert batch["status"] == "ACCEPTED" and batch["accepted_rows"] == len(lines) and batch["flagged_rows"] == 0
        assert test_app_db.count_applications(status="new") == len(lines)

    def test_valid_rows_submitted_invalid_rows_flagged(self, cfg, test_app_db):
        drop(cfg, "ACME_BROKERS_20260115_001.csv", ERRORS_FIXTURE.read_bytes())
        [batch] = intake.scan_inbox(test_app_db, cfg)
        assert batch["status"] == "PARTIALLY_ACCEPTED"
        assert (batch["total_rows"], batch["accepted_rows"], batch["flagged_rows"]) == (16, 2, 14)

        rows = {r["row_number"]: r for r in test_app_db.list_intake_rows(batch["id"])}
        accepted = [r for r in rows.values() if r["status"] == "ACCEPTED"]
        assert sorted(r["partner_reference"] for r in accepted) == ["DEMO-001", "DEMO-015"]
        assert all(r["application_id"] for r in accepted)
        assert all(not r["application_id"] for r in rows.values() if r["status"] == "FLAGGED")
        assert rows[16]["errors"] and len(rows[16]["errors"]) == 5  # every problem in the row is listed
        reported = {int(r["row_number"]): r for r in results_of(batch)}
        assert reported[16]["messages"].count(" | ") == 4 and ".;" not in reported[16]["messages"]

        # Only the valid rows became applications — in NEW status, audited as partner submissions
        new_apps = test_app_db.list_applications(status="new")
        assert len(new_apps) == 2
        quoted = next(a for a in new_apps if a["applicant_name"] == "Some Name, Jr.")
        assert quoted["loan_purpose"] == "Debt Consolidation" and quoted["fcra_consent"] is False
        trail = test_app_db.get_audit_trail(quoted["application_id"])
        assert trail[0]["event_type"] == "APPLICATION_SUBMITTED" and trail[0]["actor"] == "PARTNER:ACME_BROKERS"
        assert "DEMO-015" in trail[0]["details"] and "ACME_BROKERS_20260115_001.csv" in trail[0]["details"]

    def test_files_move_through_the_folders_and_partner_gets_results(self, cfg, test_app_db):
        src = drop(cfg, "batch_a.csv", "\n".join([HEADER, row(ref="A-1"), row(ref="A-2", ssn="12")]))
        [batch] = intake.scan_inbox(test_app_db, cfg)
        assert not src.exists()
        assert Path(batch["stored_path"]).parent == cfg.archive_dir / PARTNER and Path(batch["stored_path"]).exists()
        assert not any((cfg.processing_dir / PARTNER).iterdir())
        results = results_of(batch)
        assert Path(batch["results_path"]).parent == cfg.outbox_dir / PARTNER
        assert Path(batch["results_path"]).name == f"batch_a.batch-{batch['id']}.results.csv"
        assert [(r["partner_reference"], r["status"]) for r in results] == [("A-1", "ACCEPTED"), ("A-2", "FLAGGED")]
        assert results[0]["application_id"].startswith("APP-") and "ssn_last_4" in results[1]["messages"]
        # results never carry borrower PII
        text = Path(batch["results_path"]).read_text()
        assert "Ms. Some Name" not in text and "96000" not in text

    def test_rejected_file_submits_nothing(self, cfg, test_app_db):
        drop(cfg, "bad_header.csv", "ref,name\nR1,Some Name\n")
        [batch] = intake.scan_inbox(test_app_db, cfg)
        assert batch["status"] == "REJECTED" and "missing column(s)" in batch["error_message"]
        assert Path(batch["stored_path"]).parent == cfg.rejected_dir / PARTNER
        assert test_app_db.count_applications() == 0
        assert results_of(batch)[0]["status"] == "FILE_REJECTED"

    def test_oversized_file_rejected(self, cfg, test_app_db):
        drop(cfg, "big.csv", HEADER + "\n" + row() + "\n" + (" " * (cfg.max_file_bytes + 1)))
        [batch] = intake.scan_inbox(test_app_db, cfg)
        assert batch["status"] == "REJECTED" and "maximum is" in batch["error_message"]

    def test_already_accepted_reference_is_not_submitted_twice(self, cfg, test_app_db):
        drop(cfg, "day1.csv", "\n".join([HEADER, row(ref="REF-1")]))
        [first] = intake.scan_inbox(test_app_db, cfg)
        drop(cfg, "day2.csv", "\n".join([HEADER, row(ref="REF-1", name="Changed Name"), row(ref="REF-2")]))
        [second] = intake.scan_inbox(test_app_db, cfg)
        assert (second["accepted_rows"], second["flagged_rows"]) == (1, 1)
        dup = next(r for r in test_app_db.list_intake_rows(second["id"]) if r["partner_reference"] == "REF-1")
        first_app = test_app_db.list_intake_rows(first["id"])[0]["application_id"]
        assert dup["status"] == "FLAGGED" and first_app in dup["errors"][0]
        assert test_app_db.count_applications() == 2

    def test_references_are_scoped_per_partner(self, cfg, test_app_db):
        drop(cfg, "a.csv", "\n".join([HEADER, row(ref="SAME-1")]), partner=PARTNER)
        drop(cfg, "b.csv", "\n".join([HEADER, row(ref="SAME-1")]), partner="SUMMIT_FINANCIAL")
        batches = intake.scan_inbox(test_app_db, cfg)
        assert sorted(b["status"] for b in batches) == ["ACCEPTED", "ACCEPTED"]

    def test_identical_file_resent_is_not_reprocessed(self, cfg, test_app_db):
        content = "\n".join([HEADER, row(ref="X-1")])
        drop(cfg, "orig.csv", content)
        [first] = intake.scan_inbox(test_app_db, cfg)
        drop(cfg, "orig_resent.csv", content)
        [second] = intake.scan_inbox(test_app_db, cfg)
        assert second["status"] == "DUPLICATE_FILE" and f"batch #{first['id']}" in second["error_message"]
        assert test_app_db.count_applications() == 1

    def test_identical_resend_after_a_rejection_is_processed(self, cfg, test_app_db):
        """A file rejected for a file-level limit is evaluated again when re-sent (e.g. after the limit changed)."""
        content = "\n".join([HEADER] + [row(ref=f"L-{i}") for i in range(3)])
        small = IntakeConfig(**{**cfg.__dict__, "max_rows": 2})
        drop(cfg, "big.csv", content)
        [first] = intake.scan_inbox(test_app_db, small)
        drop(cfg, "big.csv", content)
        [second] = intake.scan_inbox(test_app_db, cfg)
        assert first["status"] == "REJECTED" and second["status"] == "ACCEPTED"

    def test_unapproved_folder_and_inbox_root_are_rejected_unread(self, cfg, test_app_db):
        drop(cfg, "x.csv", "\n".join([HEADER, row()]), partner="EVIL_CORP")
        drop(cfg, "y.csv", "\n".join([HEADER, row()]), partner=None)
        batches = {b["partner_id"]: b for b in intake.scan_inbox(test_app_db, cfg)}
        assert batches["EVIL_CORP"]["status"] == "REJECTED" and "not belong to an approved partner" in batches["EVIL_CORP"]["error_message"]
        assert batches["UNASSIGNED"]["status"] == "REJECTED"
        assert Path(batches["EVIL_CORP"]["stored_path"]).parent == cfg.rejected_dir / "_unapproved" / "EVIL_CORP"
        assert batches["EVIL_CORP"]["results_path"] is None and batches["EVIL_CORP"]["sha256"] is None  # never read
        assert test_app_db.count_applications() == 0

    def test_uploads_in_progress_are_left_alone(self, cfg, test_app_db):
        settling = IntakeConfig(**{**cfg.__dict__, "settle_seconds": 60})
        part = drop(cfg, "upload.csv.part", "\n".join([HEADER, row()]))
        fresh = drop(cfg, "just_renamed.csv", "\n".join([HEADER, row()]))
        assert intake.scan_inbox(test_app_db, settling) == []
        assert part.exists() and fresh.exists()
        old = time.time() - 120
        os.utime(fresh, (old, old))  # now settled
        [batch] = intake.scan_inbox(test_app_db, settling)
        assert batch["file_name"] == "just_renamed.csv" and part.exists()

    def test_results_file_neutralises_formula_injection(self, cfg, test_app_db):
        drop(cfg, "inj.csv", "\n".join([HEADER, row(ref="=HYPERLINK(\"http://x\")")]))
        [batch] = intake.scan_inbox(test_app_db, cfg)
        assert results_of(batch)[0]["partner_reference"].startswith("'=")

    def test_interrupted_file_is_requeued_without_double_submission(self, cfg, test_app_db):
        """
        Crash after some rows were submitted but before the batch was finalised: the file is still in processing/
        and its batch record is still RECEIVED. Recovery re-queues it; the re-run must not submit any row twice.
        """
        drop(cfg, "crash.csv", "\n".join([HEADER, row(ref="C-1"), row(ref="C-2")]))
        [batch] = intake.scan_inbox(test_app_db, cfg)
        assert test_app_db.count_applications() == 2
        stored = Path(batch["stored_path"])
        (cfg.processing_dir / PARTNER).mkdir(parents=True, exist_ok=True)
        shutil.move(str(stored), cfg.processing_dir / PARTNER / stored.name)
        test_app_db.conn.execute("UPDATE intake_batches SET status = 'RECEIVED' WHERE id = ?", (batch["id"],))
        test_app_db.conn.commit()

        assert intake.recover_interrupted(test_app_db, cfg) == 1
        assert (cfg.inbox_dir / PARTNER / "crash.csv").exists()
        assert test_app_db.get_intake_batch(batch["id"])["status"] == "INTERRUPTED"

        [again] = intake.scan_inbox(test_app_db, cfg)
        assert again["status"] == "FLAGGED"  # processed again (not skipped as a duplicate file)...
        assert all("already accepted as application" in r["errors"][0] for r in test_app_db.list_intake_rows(again["id"]))
        assert test_app_db.count_applications() == 2  # ...but nothing was submitted twice

    def test_accepted_applications_are_picked_up_by_initiate_workflow(self, cfg, test_app_db, test_checkpointer):
        drop(cfg, SAMPLE.name, SAMPLE.read_bytes())
        intake.scan_inbox(test_app_db, cfg)
        summary = workflow_service.run_batch(test_app_db, test_checkpointer)
        assert summary["selected"] == summary["found"] == 5 and summary["errors"] == 0
        assert test_app_db.count_applications(status="new") == 0
        statuses = {a["status"] for a in test_app_db.list_applications()}
        assert statuses <= {"AUTO_APPROVED", "AUTO_DENIED", "PENDING_REVIEW"} and len(statuses) == 3


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class TestConfiguration:
    def test_shipped_config_and_env_overrides(self, monkeypatch, tmp_path):
        cfg = load_intake_config(ROOT / "batch_intake.ini")
        assert cfg.enabled and cfg.max_rows == 1000 and "ACME_BROKERS" in cfg.approved_partners
        monkeypatch.setenv("BATCH_INTAKE_ENABLED", "false")
        monkeypatch.setenv("BATCH_INTAKE_ROOT", str(tmp_path))
        cfg = load_intake_config(ROOT / "batch_intake.ini")
        assert cfg.enabled is False and cfg.root_dir == tmp_path

    def test_invalid_partner_ids_are_ignored(self, tmp_path):
        ini = tmp_path / "i.ini"
        ini.write_text("[approved_partners]\nGOOD_ONE = Good\n../etc = Bad\nx = too short\n")
        assert list(load_intake_config(ini).approved_partners) == ["GOOD_ONE"]


# ---------------------------------------------------------------------------
# Admin API and the background monitor
# ---------------------------------------------------------------------------
@pytest.fixture
def admin_client(cfg, test_app_db, test_checkpointer) -> Generator[TestClient, None, None]:
    app = create_app(scheduler_enabled=False, intake_enabled=False, intake_config=cfg)
    app.dependency_overrides[get_db] = lambda: test_app_db
    app.dependency_overrides[get_checkpointer] = lambda: test_checkpointer
    with TestClient(app) as c:
        yield c


class TestAdminApi:
    def test_status_scan_list_and_detail(self, admin_client, cfg):
        st = admin_client.get("/api/v1/admin/intake").json()
        assert st["monitoring"] is False and st["columns"] == intake.COLUMNS
        assert {p["partner_id"] for p in st["partners"]} == {PARTNER, "SUMMIT_FINANCIAL"}

        drop(cfg, "errors.csv", ERRORS_FIXTURE.read_bytes())
        assert admin_client.get("/api/v1/admin/intake").json()["partners"][0]["pending_files"] == 1
        scan = admin_client.post("/api/v1/admin/intake/scan").json()
        [b] = scan["batches"]
        assert b["status"] == "PARTIALLY_ACCEPTED"
        assert admin_client.get("/api/v1/admin/intake").json()["last_scan_at"]

        listed = admin_client.get("/api/v1/admin/intake/batches").json()
        assert listed[0]["id"] == b["id"]
        detail = admin_client.get(f"/api/v1/admin/intake/batches/{b['id']}").json()
        assert len(detail["rows"]) == 16 and sum(r["status"] == "FLAGGED" for r in detail["rows"]) == 14
        assert admin_client.get("/api/v1/admin/intake/batches/999").status_code == 404

        # Accepted rows are ordinary NEW applications for the rest of the system
        new = admin_client.get("/api/v1/applications?status=new").json()
        assert len(new) == 2

    def test_admin_page_has_intake_tab(self, admin_client):
        html = admin_client.get("/admin").text
        assert 'data-tab="intake"' in html and 'id="btn-intake-scan"' in html
        assert "adminIntakeScan" in admin_client.get("/static/js/api.js").text


class TestBackgroundMonitor:
    def test_monitor_picks_up_dropped_files(self, cfg, test_checkpointer):
        import loan_agent.db as db_module

        db = db_module.get_application_database()  # the instance the lifespan uses (isolated by conftest)
        app = create_app(scheduler_enabled=False, intake_enabled=True, intake_config=cfg)
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            assert c.get("/api/v1/admin/intake").json()["monitoring"] is True
            drop(cfg, SAMPLE.name, SAMPLE.read_bytes())
            deadline = time.time() + 10
            batches = []
            while time.time() < deadline and not (batches and batches[0]["status"] != "RECEIVED"):
                batches = db.list_intake_batches()
                time.sleep(0.1)
            [batch] = batches
            assert batch["status"] == "ACCEPTED" and batch["accepted_rows"] == 5
        assert app.state.intake_task.done()  # stopped cleanly with the service

    def test_background_jobs_off_means_no_monitor_by_default(self, cfg):
        assert create_app(scheduler_enabled=False, intake_config=cfg).state.intake_enabled is False
        assert create_app(scheduler_enabled=True, intake_config=cfg).state.intake_enabled is True
