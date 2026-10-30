"""
LangGraph Checkpointer Manager.
Persists the state of the graph in a distinct database dedicated solely to checkpointing.
Supports local SQLite checkpointer (SqliteSaver) and remote PostgreSQL checkpointer (PostgresSaver).
"""
from pathlib import Path
from typing import Optional, Any
import os
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from loan_agent.config import DatabaseConfig, load_db_config


_GLOBAL_CHECKPOINTER: Optional[Any] = None


def get_langgraph_checkpointer(
    config: Optional[DatabaseConfig] = None,
    in_memory: bool = False,
    force_new: bool = False,
) -> Any:
    """
    Factory / singleton providing a persistent LangGraph checkpointer.
    Persists graph state in a dedicated database distinct from the application database.
    """
    global _GLOBAL_CHECKPOINTER

    if in_memory:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver

    if _GLOBAL_CHECKPOINTER is not None and not force_new:
        return _GLOBAL_CHECKPOINTER

    cfg = config or load_db_config()

    if cfg.is_remote:
        from langgraph.checkpoint.postgres import PostgresSaver

        saver = PostgresSaver.from_conn_string(cfg.remote_langgraph_db_url)
        saver.setup()
        _GLOBAL_CHECKPOINTER = saver
        return _GLOBAL_CHECKPOINTER
    else:
        db_path = Path(cfg.local_langgraph_db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        _GLOBAL_CHECKPOINTER = saver
        return _GLOBAL_CHECKPOINTER
