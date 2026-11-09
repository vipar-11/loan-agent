"""
Partner batch intake: approved third parties deliver loan applications in bulk as CSV files over FTP.

File format: partner_kit/LOAN_BATCH_FILE_SPEC.md (published to partners with partner_kit/loan_batch_sample.csv).
Configuration: batch_intake.ini (monitored root folder, timings, limits, approved partners).

Folder layout under <root_dir>:
    inbox/<PARTNER_ID>/        partners upload here (their FTP home). Only `*.csv` files are read; upload under a
                               temporary name (e.g. `*.csv.part`) and rename when complete.
    processing/<PARTNER_ID>/   a file is claimed by an atomic rename into here, so it is processed exactly once
    archive/<PARTNER_ID>/      files that were read (every row either accepted or flagged)
    rejected/<PARTNER_ID>/     files rejected as a whole (unreadable, wrong header, too large, duplicate file, ...)
                               plus rejected/_unapproved/<FOLDER>/ and rejected/_unassigned/ for unknown senders
    outbox/<PARTNER_ID>/       one results file per processed file, for the partner to collect

Outcome per row: ACCEPTED (submitted as a NEW application — later picked up by the initiate_workflow job) or
FLAGGED (not submitted; the reasons are recorded and reported back). Rows are validated with the same
`ApplicationCreate` model as the Applicant Portal, plus strict CSV formats, so both channels apply identical rules.
"""
import asyncio
import csv
import hashlib
import io
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from api.schemas import LOAN_AMOUNT_MAX, LOAN_AMOUNT_MIN, LOAN_PURPOSES, ApplicationCreate
from api.services import applications as app_service
from loan_agent.config import IntakeConfig, load_intake_config
from loan_agent.db import ApplicationDatabase

logger = logging.getLogger("apex.intake")

SPEC_VERSION = "1.0"
COLUMNS: List[str] = [
    "partner_reference",
    "applicant_name",
    "ssn_last_4",
    "stated_annual_income",
    "loan_amount_requested",
    "loan_purpose",
    "fcra_consent",
]
MAX_STATED_INCOME = 10_000_000.0
REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AMOUNT_PATTERN = re.compile(r"^\d+(\.\d{1,2})?$")
SSN4_PATTERN = re.compile(r"^\d{4}$")
CONSENT_VALUES = {"Y": True, "N": False}
PURPOSES_BY_KEY = {p.lower(): p for p in LOAN_PURPOSES}

TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"
SETTING_LAST_SCAN = "batch_intake.last_scan_at"
UNAPPROVED_DIR = "_unapproved"
UNASSIGNED_DIR = "_unassigned"
RESULTS_COLUMNS = ["row_number", "partner_reference", "status", "application_id", "messages"]
MESSAGE_SEPARATOR = " | "  # between the problems reported for one row in the results file

# Batch statuses
ACCEPTED, PARTIALLY_ACCEPTED, FLAGGED = "ACCEPTED", "PARTIALLY_ACCEPTED", "FLAGGED"
REJECTED, DUPLICATE_FILE, FAILED, INTERRUPTED = "REJECTED", "DUPLICATE_FILE", "FAILED", "INTERRUPTED"

_SCAN_LOCK = threading.Lock()  # one scan at a time per process (the monitor loop and "scan now" share it)


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _fmt(dt: datetime) -> str:
    return dt.strftime(TIMESTAMP_FMT)


# ---------------------------------------------------------------------------
# Parsing & validation (pure functions — no I/O)
# ---------------------------------------------------------------------------
class FileRejected(Exception):
    """The file as a whole cannot be processed; no row is submitted."""


@dataclass
class ParsedRow:
    row_number: int  # 1-based position of the record after the header (blank lines are ignored)
    partner_reference: Optional[str]
    application: Optional[ApplicationCreate] = None
    errors: List[str] = field(default_factory=list)


def _amount(raw: str, column: str, errors: List[str], low: float, high: float, low_inclusive: bool = True) -> Optional[float]:
    if not AMOUNT_PATTERN.match(raw):
        errors.append(f"{column}: '{raw}' is not a plain decimal number (digits with up to 2 decimals; "
                      f"no currency symbols or thousands separators).")
        return None
    value = float(raw)
    too_low = value < low if low_inclusive else value <= low
    if too_low or value > high:
        bound = f"at least {low:,.2f}" if low_inclusive else f"greater than {low:,.2f}"
        errors.append(f"{column}: {value:,.2f} is out of range (must be {bound} and at most {high:,.2f}).")
        return None
    return value


def validate_record(record: Dict[str, Optional[str]], row_number: int) -> ParsedRow:
    """Validates one CSV record (header-keyed, values already stripped). Collects every problem, not just the first."""
    errors: List[str] = []
    values = {c: (record.get(c) or "").strip() for c in COLUMNS}
    for column in COLUMNS:
        if not values[column]:
            errors.append(f"{column}: required value is missing.")

    reference = values["partner_reference"] or None
    if reference and not REFERENCE_PATTERN.match(reference):
        errors.append("partner_reference: must be 1-64 characters: letters, digits, '.', '_' or '-' "
                      "(starting with a letter or digit).")

    ssn = values["ssn_last_4"]
    if ssn and not SSN4_PATTERN.match(ssn):
        errors.append("ssn_last_4: must be exactly 4 digits (keep leading zeros, e.g. 0420).")

    income = _amount(values["stated_annual_income"], "stated_annual_income", errors, 0.0, MAX_STATED_INCOME,
                     low_inclusive=False) if values["stated_annual_income"] else None
    amount = _amount(values["loan_amount_requested"], "loan_amount_requested", errors, LOAN_AMOUNT_MIN,
                     LOAN_AMOUNT_MAX) if values["loan_amount_requested"] else None

    purpose = None
    if values["loan_purpose"]:
        purpose = PURPOSES_BY_KEY.get(values["loan_purpose"].lower())
        if purpose is None:
            errors.append(f"loan_purpose: '{values['loan_purpose']}' is not one of: {', '.join(LOAN_PURPOSES)}.")

    consent = None
    if values["fcra_consent"]:
        consent = CONSENT_VALUES.get(values["fcra_consent"].upper())
        if consent is None:
            errors.append(f"fcra_consent: '{values['fcra_consent']}' must be Y or N.")

    application = None
    if not errors:
        # Same model (and therefore the same rules) as the Applicant Portal submission.
        try:
            application = ApplicationCreate(
                applicant_name=values["applicant_name"],
                ssn_last_4=ssn,
                stated_annual_income=income,
                loan_amount_requested=amount,
                loan_purpose=purpose,
                fcra_consent=consent,
            )
        except ValidationError as exc:
            for err in exc.errors():
                column = str(err["loc"][0]) if err.get("loc") else "row"
                errors.append(f"{column}: {err['msg']}")
    return ParsedRow(row_number=row_number, partner_reference=reference, application=application, errors=errors)


def parse_batch(content: bytes, max_rows: int) -> List[ParsedRow]:
    """
    Parses and validates a whole batch file. Raises FileRejected for file-level problems; otherwise returns one
    ParsedRow per record, including in-file duplicate-reference checks.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileRejected(f"File is not valid UTF-8 text (byte offset {exc.start}).") from exc
    if "\x00" in text:
        raise FileRejected("File contains NUL bytes; it does not look like a CSV text file.")

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader, None)
        while header is not None and not any(h.strip() for h in header):
            header = next(reader, None)  # tolerate leading blank lines
        if header is None:
            raise FileRejected("File is empty: a header row followed by at least one application row is required.")

        names = [h.strip().lower() for h in header]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        missing = [c for c in COLUMNS if c not in names]
        unknown = [n for n in names if n not in COLUMNS]
        problems = []
        if missing:
            problems.append(f"missing column(s): {', '.join(missing)}")
        if unknown:
            problems.append(f"unknown column(s): {', '.join(repr(u) for u in unknown)}")
        if duplicates:
            problems.append(f"duplicate column(s): {', '.join(duplicates)}")
        if problems:
            raise FileRejected(f"Invalid header row ({'; '.join(problems)}). Expected exactly: {','.join(COLUMNS)}.")

        rows: List[ParsedRow] = []
        for fields in reader:
            if not any(f.strip() for f in fields):
                continue  # blank line
            row_number = len(rows) + 1
            if row_number > max_rows:
                raise FileRejected(f"File has more than the maximum of {max_rows} application rows; split it into "
                                   f"several files.")
            if len(fields) != len(names):
                rows.append(ParsedRow(
                    row_number=row_number,
                    partner_reference=None,
                    errors=[f"row: has {len(fields)} field(s) but the header has {len(names)} "
                            f"(check for unquoted commas)."],
                ))
                continue
            rows.append(validate_record(dict(zip(names, fields)), row_number))
    except csv.Error as exc:
        raise FileRejected(f"Malformed CSV near line {reader.line_num}: {exc}.") from exc

    if not rows:
        raise FileRejected("File contains a header row but no application rows.")

    # A reference used more than once in the same file is ambiguous: flag every occurrence.
    seen: Dict[str, List[int]] = {}
    for r in rows:
        if r.partner_reference:
            seen.setdefault(r.partner_reference, []).append(r.row_number)
    for r in rows:
        occurrences = seen.get(r.partner_reference or "", [])
        if len(occurrences) > 1:
            r.errors.append(f"partner_reference: '{r.partner_reference}' appears more than once in this file "
                            f"(rows {', '.join(map(str, occurrences))}).")
            r.application = None
    return rows


# ---------------------------------------------------------------------------
# Results file (returned to the partner via its outbox)
# ---------------------------------------------------------------------------
def _safe_cell(value: Any) -> str:
    """Neutralizes spreadsheet formula injection in values echoed back to partners."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _write_results(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(RESULTS_COLUMNS)
        for r in rows:
            writer.writerow([_safe_cell(r.get(c)) for c in RESULTS_COLUMNS])
    os.replace(tmp, path)  # partners never see a half-written results file


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------
def _unique_name(original: str) -> str:
    return f"{_now().strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(3)}__{original}"


def _original_name(stored: str) -> str:
    return stored.split("__", 1)[1] if "__" in stored else stored


def _move(src: Path, dest_dir: Path, name: Optional[str] = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (name or src.name)
    os.replace(src, dest)
    return dest


def ensure_folders(cfg: IntakeConfig) -> None:
    for d in (cfg.inbox_dir, cfg.processing_dir, cfg.archive_dir, cfg.rejected_dir, cfg.outbox_dir):
        d.mkdir(parents=True, exist_ok=True)
    for partner_id in cfg.approved_partners:
        (cfg.inbox_dir / partner_id).mkdir(parents=True, exist_ok=True)
        (cfg.outbox_dir / partner_id).mkdir(parents=True, exist_ok=True)


def _is_candidate(path: Path) -> bool:
    return path.is_file() and not path.name.startswith(".") and path.suffix.lower() == ".csv"


def _is_settled(path: Path, cfg: IntakeConfig, now_ts: float) -> bool:
    """True once the file has not been modified for settle_seconds (its upload is complete)."""
    try:
        return now_ts - path.stat().st_mtime >= cfg.settle_seconds
    except FileNotFoundError:
        return False


def pending_files(cfg: IntakeConfig, partner_id: str) -> List[Path]:
    folder = cfg.inbox_dir / partner_id
    return sorted((p for p in folder.iterdir() if _is_candidate(p)), key=lambda p: p.name) if folder.is_dir() else []


# ---------------------------------------------------------------------------
# Processing one file
# ---------------------------------------------------------------------------
def _finish_rejected(
    db: ApplicationDatabase, cfg: IntakeConfig, batch: Dict[str, Any], claimed: Path, partner_folder: str,
    reason: str, status: str = REJECTED, write_results: bool = True,
) -> Dict[str, Any]:
    stored = _move(claimed, cfg.rejected_dir / partner_folder)
    results_path = None
    if write_results:
        results_path = cfg.outbox_dir / partner_folder / f"{Path(batch['file_name']).stem}.batch-{batch['id']}.results.csv"
        file_status = "FILE_REJECTED" if status == REJECTED else status
        _write_results(results_path, [{"row_number": "", "partner_reference": "", "status": file_status,
                                       "application_id": "", "messages": reason}])
    logger.warning("Intake batch #%s (%s/%s) %s: %s", batch["id"], batch["partner_id"], batch["file_name"], status, reason)
    return db.update_intake_batch(batch["id"], {
        "status": status, "processed_at": _fmt(_now()), "error_message": reason,
        "stored_path": str(stored), "results_path": str(results_path) if results_path else None,
    })  # type: ignore[return-value]


def process_claimed_file(
    db: ApplicationDatabase, cfg: IntakeConfig, partner_id: str, claimed: Path, received_at: str
) -> Dict[str, Any]:
    """Processes a file already claimed into processing/<partner>/. Never raises; returns the batch record."""
    original = _original_name(claimed.name)
    batch = db.create_intake_batch(partner_id, original, received_at)
    try:
        content = claimed.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        batch = db.update_intake_batch(batch["id"], {"sha256": digest, "file_bytes": len(content)})  # type: ignore[assignment]

        if len(content) > cfg.max_file_bytes:
            return _finish_rejected(db, cfg, batch, claimed, partner_id,
                                    f"File is {len(content):,} bytes; the maximum is {cfg.max_file_bytes:,} bytes.")
        # Only files whose rows were evaluated count as "already processed" (see find_processed_intake_file).
        earlier = db.find_processed_intake_file(partner_id, digest)
        if earlier:
            return _finish_rejected(
                db, cfg, batch, claimed, partner_id,
                f"Identical to '{earlier['file_name']}' received {earlier['received_at']} (batch #{earlier['id']}); "
                f"not processed again. Send new or corrected rows in a new file.",
                status=DUPLICATE_FILE,
            )
        try:
            parsed = parse_batch(content, cfg.max_rows)
        except FileRejected as exc:
            return _finish_rejected(db, cfg, batch, claimed, partner_id, str(exc))

        partner_name = cfg.approved_partners.get(partner_id, partner_id)
        results: List[Dict[str, Any]] = []
        accepted = flagged = 0
        for row in parsed:
            app_id = None
            if not row.errors and row.partner_reference:
                previous = db.find_accepted_partner_reference(partner_id, row.partner_reference)
                if previous:
                    row.errors.append(
                        f"partner_reference: '{row.partner_reference}' was already accepted as application "
                        f"{previous['application_id']} (batch #{previous['batch_id']}); not submitted again.")
            if not row.errors and row.application is not None:
                record = app_service.create_application(
                    db, row.application,
                    actor=f"PARTNER:{partner_id}",
                    details=(f"Application submitted by approved partner {partner_name} ({partner_id}) via batch file "
                             f"'{original}' (intake batch #{batch['id']}, row {row.row_number}, partner reference "
                             f"{row.partner_reference}); queued for approval workflow initiation."),
                )
                app_id = record["application_id"]
                try:
                    db.add_intake_row(batch["id"], row.row_number, partner_id, row.partner_reference, "ACCEPTED", app_id)
                except Exception:  # pragma: no cover - unique index backstop for a concurrent duplicate
                    db.conn.rollback()
                    db.add_intake_row(batch["id"], row.row_number, partner_id, row.partner_reference, "FLAGGED",
                                      app_id, ["partner_reference: accepted concurrently by another batch."])
                accepted += 1
                results.append({"row_number": row.row_number, "partner_reference": row.partner_reference,
                                "status": "ACCEPTED", "application_id": app_id, "messages": ""})
            else:
                db.add_intake_row(batch["id"], row.row_number, partner_id, row.partner_reference, "FLAGGED",
                                  None, row.errors)
                flagged += 1
                results.append({"row_number": row.row_number, "partner_reference": row.partner_reference,
                                "status": "FLAGGED", "application_id": "", "messages": MESSAGE_SEPARATOR.join(row.errors)})

        status = ACCEPTED if not flagged else (FLAGGED if not accepted else PARTIALLY_ACCEPTED)
        stored = _move(claimed, cfg.archive_dir / partner_id)
        results_path = cfg.outbox_dir / partner_id / f"{Path(original).stem}.batch-{batch['id']}.results.csv"
        _write_results(results_path, results)
        logger.info("Intake batch #%s (%s/%s): %s — %d accepted, %d flagged",
                    batch["id"], partner_id, original, status, accepted, flagged)
        return db.update_intake_batch(batch["id"], {
            "status": status, "processed_at": _fmt(_now()), "total_rows": len(parsed),
            "accepted_rows": accepted, "flagged_rows": flagged,
            "stored_path": str(stored), "results_path": str(results_path),
        })  # type: ignore[return-value]
    except Exception as exc:  # defensive: leave the file for investigation, never crash the monitor
        logger.exception("Intake batch #%s failed", batch["id"])
        try:
            stored = _move(claimed, cfg.rejected_dir / partner_id) if claimed.exists() else None
        except OSError:
            stored = None
        return db.update_intake_batch(batch["id"], {
            "status": FAILED, "processed_at": _fmt(_now()), "error_message": f"Unexpected error: {exc}",
            "stored_path": str(stored) if stored else None,
        })  # type: ignore[return-value]


def _claim(path: Path, dest_dir: Path) -> Optional[Path]:
    """Atomically moves an inbox file into processing/. Returns None if someone else claimed it first."""
    try:
        return _move(path, dest_dir, _unique_name(path.name))
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Scanning the inbox
# ---------------------------------------------------------------------------
def scan_inbox(db: ApplicationDatabase, cfg: Optional[IntakeConfig] = None, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    One pass over the inbox. Processes every settled `*.csv` file in each approved partner's folder, and rejects
    files found in unapproved folders or directly in the inbox root. Returns the batch records created.
    """
    cfg = cfg or load_intake_config()
    now_ts = now.timestamp() if now else time.time()  # sub-second precision for the settle check
    now = now or _now()
    batches: List[Dict[str, Any]] = []
    with _SCAN_LOCK:
        ensure_folders(cfg)
        for entry in sorted(cfg.inbox_dir.iterdir(), key=lambda p: p.name):
            if entry.is_dir():
                folder = entry.name
                approved = folder in cfg.approved_partners
                for path in pending_files(cfg, folder):
                    if not _is_settled(path, cfg, now_ts):
                        continue  # upload may still be in progress
                    received_at = _fmt(datetime.fromtimestamp(path.stat().st_mtime))
                    if approved:
                        claimed = _claim(path, cfg.processing_dir / folder)
                        if claimed:
                            batches.append(process_claimed_file(db, cfg, folder, claimed, received_at))
                    else:
                        claimed = _claim(path, cfg.processing_dir / UNAPPROVED_DIR / folder)
                        if claimed:
                            batch = db.create_intake_batch(folder[:64], path.name, received_at)
                            batches.append(_finish_rejected(
                                db, cfg, batch, claimed, f"{UNAPPROVED_DIR}/{folder}",
                                f"Folder '{folder}' does not belong to an approved partner; file not read.",
                                write_results=False,
                            ))
            elif _is_candidate(entry) and _is_settled(entry, cfg, now_ts):
                claimed = _claim(entry, cfg.processing_dir / UNASSIGNED_DIR)
                if claimed:
                    received_at = _fmt(datetime.fromtimestamp(claimed.stat().st_mtime))
                    batch = db.create_intake_batch("UNASSIGNED", entry.name, received_at)
                    batches.append(_finish_rejected(
                        db, cfg, batch, claimed, UNASSIGNED_DIR,
                        "File was placed directly in the inbox root; files must be delivered to the partner's own "
                        "inbox folder. File not read.",
                        write_results=False,
                    ))
        db.set_setting(SETTING_LAST_SCAN, _fmt(now), updated_by="BATCH_INTAKE")
    return batches


def recover_interrupted(db: ApplicationDatabase, cfg: Optional[IntakeConfig] = None) -> int:
    """
    Startup recovery: a file left in processing/ means the service stopped mid-file. It is returned to the inbox and
    processed again; rows that were already submitted are recognised by their partner_reference and reported as
    duplicates instead of being submitted twice. Returns the number of files re-queued.
    """
    cfg = cfg or load_intake_config()
    requeued = 0
    with _SCAN_LOCK:
        if not cfg.processing_dir.is_dir():
            return 0
        for partner_dir in cfg.processing_dir.iterdir():
            if not partner_dir.is_dir() or partner_dir.name not in cfg.approved_partners:
                continue
            for path in partner_dir.iterdir():
                if path.is_file():
                    _move(path, cfg.inbox_dir / partner_dir.name, _original_name(path.name))
                    requeued += 1
        for batch in db.list_intake_batches(limit=500):
            if batch["status"] == "RECEIVED":
                db.update_intake_batch(batch["id"], {
                    "status": INTERRUPTED, "processed_at": _fmt(_now()),
                    "error_message": "Processing was interrupted (service stopped); the file was re-queued.",
                })
    if requeued:
        logger.warning("Batch intake: re-queued %d interrupted file(s)", requeued)
    return requeued


# ---------------------------------------------------------------------------
# Monitor (background task started by the application lifespan)
# ---------------------------------------------------------------------------
async def intake_loop(db: ApplicationDatabase, stop: asyncio.Event, cfg: Optional[IntakeConfig] = None) -> None:
    cfg = cfg or load_intake_config()
    logger.info("Batch intake monitor started: %s (every %ss)", cfg.inbox_dir.resolve(), cfg.poll_seconds)
    await asyncio.to_thread(ensure_folders, cfg)
    await asyncio.to_thread(recover_interrupted, db, cfg)
    while not stop.is_set():
        try:
            await asyncio.to_thread(scan_inbox, db, cfg)
        except Exception:  # pragma: no cover - keep monitoring no matter what
            logger.exception("Batch intake scan failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.poll_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("Batch intake monitor stopped")


def status_snapshot(db: ApplicationDatabase, cfg: IntakeConfig, monitoring: bool) -> Dict[str, Any]:
    return {
        "enabled": cfg.enabled,
        "monitoring": monitoring,
        "spec_version": SPEC_VERSION,
        "columns": COLUMNS,
        "root_dir": str(cfg.root_dir.resolve()),
        "inbox_dir": str(cfg.inbox_dir.resolve()),
        "poll_seconds": cfg.poll_seconds,
        "settle_seconds": cfg.settle_seconds,
        "max_rows": cfg.max_rows,
        "max_file_bytes": cfg.max_file_bytes,
        "last_scan_at": db.get_setting(SETTING_LAST_SCAN),
        "partners": [
            {
                "partner_id": pid,
                "name": name,
                "inbox_dir": str((cfg.inbox_dir / pid).resolve()),
                "outbox_dir": str((cfg.outbox_dir / pid).resolve()),
                "pending_files": len(pending_files(cfg, pid)),
            }
            for pid, name in sorted(cfg.approved_partners.items())
        ],
    }
