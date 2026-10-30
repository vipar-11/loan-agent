"""
Unit tests for loan_agent/checkpointer.py.
"""
from pathlib import Path
from unittest.mock import MagicMock
import os
import pytest

from loan_agent.config import DatabaseConfig
from loan_agent.checkpointer import get_langgraph_checkpointer
import loan_agent.checkpointer as cp_mod


class TestCheckpointer:
    def test_in_memory_checkpointer(self):
        saver = get_langgraph_checkpointer(in_memory=True)
        assert saver is not None
        # Verify saver has standard LangGraph checkpoint interface
        assert hasattr(saver, "get_tuple")
        assert hasattr(saver, "put")

    def test_local_file_checkpointer(self, tmp_path):
        cp_mod._GLOBAL_CHECKPOINTER = None

        db_path = tmp_path / "checkpoints" / "lg_test.db"
        cfg = DatabaseConfig(
            mode="local",
            local_app_db_path=tmp_path / "app.db",
            local_langgraph_db_path=db_path,
            remote_app_db_url="postgresql://localhost/app",
            remote_langgraph_db_url="postgresql://localhost/lg",
        )

        saver = get_langgraph_checkpointer(config=cfg, in_memory=False, force_new=False)
        assert saver is not None
        assert db_path.exists()
        # Verify singleton caching
        cached_saver = get_langgraph_checkpointer(config=cfg, in_memory=False, force_new=False)
        assert cached_saver is saver

    def test_remote_postgres_checkpointer_mock(self, monkeypatch):
        cp_mod._GLOBAL_CHECKPOINTER = None

        mock_postgres_saver = MagicMock()
        mock_instance = MagicMock()
        mock_postgres_saver.from_conn_string.return_value = mock_instance

        monkeypatch.setattr(
            "langgraph.checkpoint.postgres.PostgresSaver", mock_postgres_saver
        )

        cfg = DatabaseConfig(
            mode="remote",
            local_app_db_path=Path("dummy_app.db"),
            local_langgraph_db_path=Path("dummy_lg.db"),
            remote_app_db_url="postgresql://localhost/app",
            remote_langgraph_db_url="postgresql://localhost/lg",
        )

        saver = get_langgraph_checkpointer(config=cfg, in_memory=False, force_new=False)
        assert saver is mock_instance
        mock_postgres_saver.from_conn_string.assert_called_once_with(cfg.remote_langgraph_db_url)
        mock_instance.setup.assert_called_once()
