import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.tools import ToolService, _statement_timeout


def test_sql_validator_allows_only_allowlisted_read_queries():
    service = ToolService(Settings(tool_allowed_tables="employees,departments"))

    service.validate("sql.read", {"query": "SELECT name FROM employees LIMIT 10"})

    with pytest.raises(ValueError, match="未授权"):
        service.validate("sql.read", {"query": "SELECT * FROM salaries"})
    with pytest.raises(ValueError, match="只读"):
        service.validate("sql.read", {"query": "DELETE FROM employees"})

    with pytest.raises(ValueError, match="尚未配置"):
        ToolService(Settings(tool_allowed_tables="")).validate(
            "sql.read", {"query": "SELECT * FROM employees"}
        )


def test_http_tool_enforces_domain_allowlist():
    service = ToolService(Settings(tool_http_allowed_domains="api.example.com"))

    service.validate("http.get", {"url": "https://api.example.com/v1/policies"})

    with pytest.raises(ValueError, match="允许列表"):
        service.validate("http.get", {"url": "https://example.net/private"})
    with pytest.raises(ValueError):
        service.validate("http.get", {"url": "file:///etc/passwd"})
    with pytest.raises(ValueError, match="HTTPS"):
        service.validate("http.get", {"url": "http://api.example.com/v1/policies"})


def test_tool_approval_executes_success_and_persists_failure(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = ToolService(Settings(tool_allowed_tables="employees"))

    with Session(engine) as db:
        successful = service.submit(
            db,
            "tenant-a",
            "user-a",
            "sql.read",
            {"query": "SELECT name FROM employees"},
        )
        monkeypatch.setattr(
            service,
            "_execute_sql",
            lambda _query: {"rows": [{"name": "Alice"}], "row_count": 1},
        )
        approval, execution = service.decide(
            db, successful.id, "tenant-a", "admin", "approve", None
        )
        assert approval.status == "completed"
        assert execution.status == "completed"
        assert execution.result["row_count"] == 1

        failing = service.submit(
            db,
            "tenant-a",
            "user-a",
            "sql.read",
            {"query": "SELECT name FROM employees"},
        )

        def fail(_query):
            raise RuntimeError("read database unavailable")

        monkeypatch.setattr(service, "_execute_sql", fail)
        approval, execution = service.decide(db, failing.id, "tenant-a", "admin", "approve", None)
        assert approval.status == "failed"
        assert execution.status == "failed"
        assert "unavailable" in execution.error_message


def test_postgres_statement_timeout_is_transaction_local():
    class FakeDialect:
        name = "postgresql"

    class FakeConnection:
        dialect = FakeDialect()

        def __init__(self):
            self.calls = []

        def execute(self, statement, parameters):
            self.calls.append((str(statement), parameters))

    connection = FakeConnection()

    with _statement_timeout(connection, 2500):
        pass

    assert "set_config('statement_timeout'" in connection.calls[0][0]
    assert connection.calls[0][1] == {"timeout": "2500ms"}
