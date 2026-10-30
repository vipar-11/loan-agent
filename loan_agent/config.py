"""
Configuration loader for Application Database and LangGraph Checkpointer.
"""
from dataclasses import dataclass
from pathlib import Path
import configparser
import os
from typing import Optional


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
    remote_app_url = "postgresql://postgres:postgres@localhost:5432/loan_app_db"
    remote_lg_url = "postgresql://postgres:postgres@localhost:5432/loan_checkpoint_db"

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
