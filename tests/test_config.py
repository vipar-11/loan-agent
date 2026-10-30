"""
Unit tests for loan_agent/config.py.
"""
from pathlib import Path
import os
import pytest

from loan_agent.config import DatabaseConfig, find_config_file, load_db_config


class TestDatabaseConfig:
    def test_database_config_properties(self):
        cfg_local = DatabaseConfig(
            mode="local",
            local_app_db_path=Path("app.db"),
            local_langgraph_db_path=Path("lg.db"),
            remote_app_db_url="postgresql://localhost/app",
            remote_langgraph_db_url="postgresql://localhost/lg",
        )
        assert cfg_local.is_local is True
        assert cfg_local.is_remote is False

        cfg_remote = DatabaseConfig(
            mode="remote",
            local_app_db_path=Path("app.db"),
            local_langgraph_db_path=Path("lg.db"),
            remote_app_db_url="postgresql://localhost/app",
            remote_langgraph_db_url="postgresql://localhost/lg",
        )
        assert cfg_remote.is_local is False
        assert cfg_remote.is_remote is True


class TestFindConfigFile:
    def test_find_config_file_explicit_exists(self, tmp_path):
        f = tmp_path / "custom_config.ini"
        f.write_text("[storage]\nmode=local\n", encoding="utf-8")
        found = find_config_file(f)
        assert found == f

    def test_find_config_file_explicit_missing(self, tmp_path):
        f = tmp_path / "non_existent.ini"
        # Should fall through to defaults or env
        found = find_config_file(f)
        # Should not be the non existent file
        assert found != f

    def test_find_config_file_env_var(self, tmp_path, monkeypatch):
        f = tmp_path / "env_config.ini"
        f.write_text("[storage]\nmode=remote\n", encoding="utf-8")
        monkeypatch.setenv("DBCONFIG_PATH", str(f))
        found = find_config_file()
        assert found == f

    def test_find_config_file_env_var_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DBCONFIG_PATH", str(tmp_path / "missing.ini"))
        # Should fall through
        found = find_config_file()
        assert found is None or found.name == "dbconfig.ini"

    def test_find_config_file_default_candidates(self):
        found = find_config_file()
        assert found is not None
        assert found.name == "dbconfig.ini"

    def test_find_config_file_none_when_empty(self, monkeypatch):
        monkeypatch.setattr(Path, "exists", lambda self: False)
        found = find_config_file()
        assert found is None


class TestLoadDbConfig:
    def test_load_db_config_from_file(self, tmp_path):
        cfg_file = tmp_path / "test_db.ini"
        cfg_file.write_text(
            """
[storage]
mode = remote

[local]
app_db_path = local_app_test.db
langgraph_db_path = local_lg_test.db

[remote]
app_db_url = postgresql://usr:pwd@remote-host:5432/app_db
langgraph_db_url = postgresql://usr:pwd@remote-host:5432/lg_db
""",
            encoding="utf-8",
        )
        cfg = load_db_config(cfg_file)
        assert cfg.mode == "remote"
        assert cfg.is_remote is True
        assert cfg.local_app_db_path == Path("local_app_test.db")
        assert cfg.local_langgraph_db_path == Path("local_lg_test.db")
        assert "app_db" in cfg.remote_app_db_url
        assert "lg_db" in cfg.remote_langgraph_db_url

    def test_load_db_config_environment_variable_overrides(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "test_db.ini"
        cfg_file.write_text("[storage]\nmode = local\n", encoding="utf-8")

        monkeypatch.setenv("APP_STORAGE_MODE", "remote")
        monkeypatch.setenv("APP_DB_PATH", "/override/app.db")
        monkeypatch.setenv("LANGGRAPH_DB_PATH", "/override/lg.db")
        monkeypatch.setenv("APP_DB_URL", "postgresql://env/app")
        monkeypatch.setenv("LANGGRAPH_DB_URL", "postgresql://env/lg")

        cfg = load_db_config(cfg_file)
        assert cfg.mode == "remote"
        assert cfg.local_app_db_path == Path("/override/app.db")
        assert cfg.local_langgraph_db_path == Path("/override/lg.db")
        assert cfg.remote_app_db_url == "postgresql://env/app"
        assert cfg.remote_langgraph_db_url == "postgresql://env/lg"

    def test_load_db_config_defaults_when_no_file(self, monkeypatch):
        monkeypatch.setattr("loan_agent.config.find_config_file", lambda explicit=None: None)
        cfg = load_db_config(None)
        assert cfg.mode == "local"
        assert cfg.is_local is True
        assert cfg.local_app_db_path == Path("data/loan_agent_app.db")
        assert cfg.local_langgraph_db_path == Path("data/loan_agent_checkpoints.db")
