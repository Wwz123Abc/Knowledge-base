from __future__ import annotations

import ipaddress
import json
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
import sqlglot
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlglot import exp

from app.config import Settings, get_settings
from app.models import ToolApproval, ToolExecution

FORBIDDEN_SQL = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Merge,
)


class ToolService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def submit(
        self,
        db: Session,
        tenant_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict,
    ) -> ToolApproval:
        self.validate(tool_name, arguments)
        approval = ToolApproval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
            status="pending",
        )
        db.add(approval)
        db.commit()
        db.refresh(approval)
        return approval

    def validate(self, tool_name: str, arguments: dict) -> None:
        if tool_name == "sql.read":
            self._validate_sql(str(arguments.get("query", "")))
            return
        if tool_name == "http.get":
            self._validate_url(str(arguments.get("url", "")), resolve_dns=False)
            return
        raise ValueError("不支持的工具")

    def list_pending(self, db: Session, tenant_id: str) -> list[ToolApproval]:
        return list(
            db.scalars(
                select(ToolApproval)
                .where(
                    ToolApproval.tenant_id == tenant_id,
                    ToolApproval.status == "pending",
                )
                .order_by(ToolApproval.created_at)
            )
        )

    def decide(
        self,
        db: Session,
        approval_id: str,
        tenant_id: str,
        admin_id: str,
        decision: str,
        note: str | None,
    ) -> tuple[ToolApproval, ToolExecution | None] | None:
        approval = db.scalar(
            select(ToolApproval).where(
                ToolApproval.id == approval_id,
                ToolApproval.tenant_id == tenant_id,
                ToolApproval.status == "pending",
            )
        )
        if not approval:
            return None
        approval.status = "approved" if decision == "approve" else "rejected"
        approval.decision_by = admin_id
        approval.decision_note = note
        approval.decided_at = datetime.now(UTC)
        db.commit()
        if decision == "reject":
            return approval, None
        execution = self.execute(db, approval)
        return approval, execution

    def execute(self, db: Session, approval: ToolApproval) -> ToolExecution:
        try:
            if approval.tool_name == "sql.read":
                execution_result = self._execute_sql(str(approval.arguments["query"]))
            else:
                execution_result = self._execute_http(str(approval.arguments["url"]))
            execution = ToolExecution(
                approval_id=approval.id,
                status="completed",
                result=execution_result,
            )
            approval.status = "completed"
        except Exception as exc:
            execution = ToolExecution(
                approval_id=approval.id,
                status="failed",
                result={},
                error_message=str(exc)[:4000],
            )
            approval.status = "failed"
        db.add(execution)
        db.commit()
        db.refresh(execution)
        return execution

    def _validate_sql(self, query: str) -> None:
        if not query.strip():
            raise ValueError("SQL 查询不能为空")
        try:
            statements = sqlglot.parse(query)
        except sqlglot.errors.ParseError as exc:
            raise ValueError("SQL 语法无效") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("只允许单条只读查询")
        if any(statements[0].find(node_type) for node_type in FORBIDDEN_SQL):
            raise ValueError("SQL 包含禁止的写入或管理操作")
        # Functions sqlglot doesn't know by name are the dangerous ones in practice
        # (pg_read_file, dblink, lo_import, nextval, pg_terminate_backend ...): a read-only
        # SELECT can still call them, and they act outside the rolled-back transaction.
        if statements[0].find(exp.Anonymous):
            raise ValueError("SQL 包含不受支持的函数")
        allowed = set(self.settings.tool_allowed_table_list)
        if not allowed:
            raise ValueError("尚未配置 SQL 工具允许表，已拒绝查询")
        # A schema-qualified name only counts when the allow-list says so explicitly
        # ("hr.employees"); "other_schema.employees" must not pass as the allowed "employees".
        tables = {
            f"{table.db}.{table.name}" if table.db else table.name
            for table in statements[0].find_all(exp.Table)
        }
        if not tables or not tables <= allowed:
            raise ValueError("SQL 访问了未授权的数据表")

    def _execute_sql(self, query: str) -> dict:
        self._validate_sql(query)
        if not self.settings.tool_database_url:
            raise RuntimeError("尚未配置只读工具数据库")
        engine = create_engine(self.settings.tool_database_url, pool_pre_ping=True)
        wrapped = f"SELECT * FROM ({query.rstrip().rstrip(';')}) AS approved_query LIMIT :limit"
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    with _statement_timeout(connection, self.settings.tool_sql_timeout_ms):
                        rows = connection.execute(
                            text(wrapped), {"limit": self.settings.tool_max_rows}
                        )
                        query_rows = [dict(row._mapping) for row in rows]
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
        return {
            "rows": query_rows,
            "row_count": len(query_rows),
            "truncated": len(query_rows) >= self.settings.tool_max_rows,
        }

    def _validate_url(self, url: str, resolve_dns: bool) -> list[str]:
        parsed = urlparse(url)
        allowed_schemes = {"https"} if self.settings.tool_http_require_https else {"https", "http"}
        if parsed.scheme not in allowed_schemes or not parsed.hostname:
            if self.settings.tool_http_require_https:
                raise ValueError("仅允许完整的 HTTPS 地址")
            raise ValueError("仅允许完整的 HTTP/HTTPS 地址")
        hostname = parsed.hostname.lower()
        allowed = self.settings.tool_http_domain_list
        if not allowed or not any(
            hostname == domain or hostname.endswith(f".{domain}") for domain in allowed
        ):
            raise ValueError("目标域名不在允许列表中")
        if resolve_dns:
            default_port = 443 if parsed.scheme == "https" else 80
            addresses = sorted(
                {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or default_port)}
            )
            if not addresses or any(
                not ipaddress.ip_address(address).is_global for address in addresses
            ):
                raise ValueError("目标地址不是允许的公网地址")
            return addresses
        return []

    def _execute_http(self, url: str) -> dict:
        addresses = self._validate_url(url, resolve_dns=True)
        parsed = urlparse(url)
        # Connect to the address that was just validated. Resolving the name a second time
        # (as httpx would) lets a DNS answer that changes between the check and the request
        # point the call at an internal host; the Host header and TLS server name still carry
        # the real hostname, so virtual hosting and certificate checks work as before.
        pinned = f"[{addresses[0]}]" if ":" in addresses[0] else addresses[0]
        netloc = f"{pinned}:{parsed.port}" if parsed.port else pinned
        pinned_url = parsed._replace(netloc=netloc).geturl()
        host_header = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
        limit = 1_000_000
        buffer = bytearray()
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            with client.stream(
                "GET",
                pinned_url,
                headers={"Accept": "application/json, text/plain", "Host": host_header},
                extensions={"sni_hostname": parsed.hostname},
            ) as response:
                response.raise_for_status()
                # Read at most `limit` bytes: a huge response used to be pulled into memory in
                # full and only then cut down.
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) >= limit:
                        break
                status_code = response.status_code
                content_type = response.headers.get("content-type", "")
        content = bytes(buffer[:limit])
        text_body = content.decode("utf-8", errors="replace")
        body: object = text_body
        if "json" in content_type:
            try:
                body = json.loads(text_body)
            except ValueError:
                body = text_body
        return {"status_code": status_code, "content_type": content_type, "body": body}


@contextmanager
def _statement_timeout(connection, timeout_ms: int) -> Iterator[None]:
    dialect = connection.dialect.name
    sqlite_connection = None
    if dialect == "postgresql":
        connection.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{timeout_ms}ms"},
        )
    elif dialect == "sqlite":
        sqlite_connection = connection.connection.driver_connection
        deadline = time.monotonic() + timeout_ms / 1000
        sqlite_connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline),
            1000,
        )
    try:
        yield
    finally:
        if sqlite_connection is not None:
            sqlite_connection.set_progress_handler(None, 0)
