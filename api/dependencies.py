"""
FastAPI dependency providers.

Resolved lazily through the module attributes of `loan_agent.db` / `loan_agent.checkpointer`
so that singletons are shared across requests and tests can swap them via
`app.dependency_overrides` (or by monkeypatching the underlying factories).
"""
import threading
from typing import Any

import loan_agent.checkpointer as checkpointer_module
import loan_agent.db as db_module
from loan_agent.db import ApplicationDatabase

# Serializes workflow executions: SQLite-backed checkpointers and the shared application
# connection are not designed for concurrent writers from a request threadpool.
WORKFLOW_LOCK = threading.Lock()


def get_db() -> ApplicationDatabase:
    """Singleton application database (applications + audit trail)."""
    return db_module.get_application_database()


def get_checkpointer() -> Any:
    """Singleton workflow checkpointer (distinct database from the application DB)."""
    return checkpointer_module.get_langgraph_checkpointer()
