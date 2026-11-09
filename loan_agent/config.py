"""
Configuration loader for Application Database and LangGraph Checkpointer.
"""
from dataclasses import dataclass
from pathlib import Path
import configparser
import os
from typing import Dict, Optional


@dataclass
class DatabaseConfig:
    mode: str  # "local" or "remote"
    local_app_db_path: Path
    local_langgraph_db_path: Path
    remote_app_db_url: str
    remote_langgraph_db_url: str

    @property
    def is_remote(self) -> bool:
        return self.mode.lower() == "remote"

    @property
    def is_local(self) -> bool:
        return not self.is_remote


def find_config_file(explicit_path: Optional[str | Path] = None) -> Optional[Path]:
    """Resolves the configuration file path from explicit argument, env, or known names."""
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return p

    env_path = os.environ.get("DBCONFIG_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    # Standard candidate file names in cwd or workspace
    candidates = [
        Path.cwd() / "dbconfig.ini",
        Path(__file__).resolve().parent.parent / "dbconfig.ini",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def load_db_config(config_path: Optional[str | Path] = None) -> DatabaseConfig:
    """
    Loads database configuration from INI file or defaults.
    Falls back gracefully if the configuration file is absent.
    """
    parser = configparser.ConfigParser()
    resolved_file = find_config_file(config_path)

    mode = "local"
    local_app_db = Path("data/loan_agent_app.db")
    local_lg_db = Path("data/loan_agent_checkpoints.db")
    remote_app_url = "postgresql://localhost:5432/loan_app_db"
    remote_lg_url = "postgresql://localhost:5432/loan_checkpoint_db"

    if resolved_file and resolved_file.exists():
        parser.read(resolved_file, encoding="utf-8")

        if parser.has_section("storage"):
            mode = parser.get("storage", "mode", fallback=mode).strip()

        if parser.has_section("local"):
            app_path_str = parser.get("local", "app_db_path", fallback=str(local_app_db)).strip()
            lg_path_str = parser.get("local", "langgraph_db_path", fallback=str(local_lg_db)).strip()
            local_app_db = Path(app_path_str)
            local_lg_db = Path(lg_path_str)

        if parser.has_section("remote"):
            remote_app_url = parser.get("remote", "app_db_url", fallback=remote_app_url).strip()
            remote_lg_url = parser.get("remote", "langgraph_db_url", fallback=remote_lg_url).strip()

    # Environment variable overrides
    if "APP_STORAGE_MODE" in os.environ:
        mode = os.environ["APP_STORAGE_MODE"].strip()
    if "APP_DB_PATH" in os.environ:
        local_app_db = Path(os.environ["APP_DB_PATH"].strip())
    if "LANGGRAPH_DB_PATH" in os.environ:
        local_lg_db = Path(os.environ["LANGGRAPH_DB_PATH"].strip())
    if "APP_DB_URL" in os.environ:
        remote_app_url = os.environ["APP_DB_URL"].strip()
    if "LANGGRAPH_DB_URL" in os.environ:
        remote_lg_url = os.environ["LANGGRAPH_DB_URL"].strip()

    return DatabaseConfig(
        mode=mode.lower(),
        local_app_db_path=local_app_db,
        local_langgraph_db_path=local_lg_db,
        remote_app_db_url=remote_app_url,
        remote_langgraph_db_url=remote_lg_url,
    )


# ---------------------------------------------------------------------------
# Partner batch intake (bulk loan applications delivered by approved third parties over FTP)
# ---------------------------------------------------------------------------
PARTNER_ID_PATTERN = r"^[A-Z0-9][A-Z0-9_]{1,31}$"


@dataclass
class IntakeConfig:
    enabled: bool
    root_dir: Path  # inbox/, processing/, archive/, rejected/ and outbox/ live under here
    poll_seconds: float  # how often the inbox is scanned
    settle_seconds: float  # a file must be unmodified this long before pickup (upload finished)
    max_file_bytes: int
    max_rows: int
    approved_partners: Dict[str, str]  # partner_id -> partner name (partner_id == inbox sub-directory)

    @property
    def inbox_dir(self) -> Path:
        return self.root_dir / "inbox"

    @property
    def processing_dir(self) -> Path:
        return self.root_dir / "processing"

    @property
    def archive_dir(self) -> Path:
        return self.root_dir / "archive"

    @property
    def rejected_dir(self) -> Path:
        return self.root_dir / "rejected"

    @property
    def outbox_dir(self) -> Path:
        return self.root_dir / "outbox"


def find_intake_config_file(explicit_path: Optional[str | Path] = None) -> Optional[Path]:
    """Resolves batch_intake.ini from an explicit path, BATCH_INTAKE_CONFIG, the cwd, or the project root."""
    for candidate in (
        Path(explicit_path) if explicit_path else None,
        Path(os.environ["BATCH_INTAKE_CONFIG"]) if os.environ.get("BATCH_INTAKE_CONFIG") else None,
        Path.cwd() / "batch_intake.ini",
        Path(__file__).resolve().parent.parent / "batch_intake.ini",
    ):
        if candidate is not None and candidate.exists():
            return candidate
    return None


def load_intake_config(config_path: Optional[str | Path] = None) -> IntakeConfig:
    """
    Loads the partner batch-intake configuration ([batch_intake] + [approved_partners] in batch_intake.ini).
    Partner IDs that do not match PARTNER_ID_PATTERN are ignored (they could not be used as inbox folder names).
    Environment overrides: BATCH_INTAKE_ENABLED, BATCH_INTAKE_ROOT.
    """
    import re

    parser = configparser.ConfigParser()
    parser.optionxform = str  # keep partner IDs exactly as written
    resolved = find_intake_config_file(config_path)
    if resolved:
        parser.read(resolved, encoding="utf-8")

    section = parser["batch_intake"] if parser.has_section("batch_intake") else {}

    def _get(key: str, default: str) -> str:
        return str(section.get(key, default)).strip()  # type: ignore[union-attr]

    enabled = _get("enabled", "true").lower() in ("1", "true", "yes", "on")
    root_dir = Path(_get("root_dir", "data/intake"))
    if os.environ.get("BATCH_INTAKE_ENABLED"):
        enabled = os.environ["BATCH_INTAKE_ENABLED"].strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("BATCH_INTAKE_ROOT"):
        root_dir = Path(os.environ["BATCH_INTAKE_ROOT"].strip())

    partners: Dict[str, str] = {}
    if parser.has_section("approved_partners"):
        for partner_id, name in parser.items("approved_partners"):
            pid = partner_id.strip().upper()
            if re.match(PARTNER_ID_PATTERN, pid):
                partners[pid] = name.strip() or pid

    return IntakeConfig(
        enabled=enabled,
        root_dir=root_dir,
        poll_seconds=max(1.0, float(_get("poll_seconds", "15"))),
        settle_seconds=max(0.0, float(_get("settle_seconds", "10"))),
        max_file_bytes=int(_get("max_file_bytes", str(5 * 1024 * 1024))),
        max_rows=int(_get("max_rows", "1000")),
        approved_partners=partners,
    )
