"""SQL-speaking connectors, all built on SQLAlchemy.

One class covers every engine SQLAlchemy has a dialect for; the per-engine
subclasses exist to carry the details that actually differ: the URL shape, the
LIMIT syntax, and how to read a row estimate without a full count.

Drivers are imported lazily. A box that never touches Snowflake should not pay
for importing it, and a missing optional driver must degrade to a clear message
rather than an ImportError at boot.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote_plus

from twohelixes.connectors.base import (
    Column,
    Connector,
    ConnectorError,
    QueryResult,
    Table,
    apply_limit,
    assert_read_only,
)

log = logging.getLogger("twohelixes.connectors.sql")


class SQLAlchemyConnector(Connector):
    kind = "sql"
    display_name = "SQL database"
    dialect = "sql"
    driver_package = ""

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.engine: Any = None
        self.inspector: Any = None

    # -- url ---------------------------------------------------------------

    def url(self) -> str:
        dsn = self.config.get("dsn") or self.config.get("url")
        if dsn:
            return str(dsn)
        return self._build_url()

    def _build_url(self) -> str:
        raise ConnectorError(f"{self.display_name} needs a connection string")

    def _credentials(self) -> tuple[str, str, str, str, str]:
        return (
            str(self.config.get("user") or ""),
            quote_plus(str(self.config.get("password") or "")),
            str(self.config.get("host") or "localhost"),
            str(self.config.get("port") or ""),
            str(self.config.get("database") or ""),
        )

    # -- lifecycle ---------------------------------------------------------

    def _connect(self) -> None:
        from sqlalchemy import create_engine, inspect

        try:
            self.engine = create_engine(
                self.url(),
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=2,
                pool_recycle=1800,
            )
        except ModuleNotFoundError as exc:
            hint = f" Install {self.driver_package}." if self.driver_package else ""
            raise ConnectorError(f"driver unavailable: {exc}.{hint}") from exc

        with self.engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        self.inspector = inspect(self.engine)

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
        super().close()

    # -- introspection -----------------------------------------------------

    def tables(self) -> list[Table]:
        if self.inspector is None and not self.connect():
            raise ConnectorError(self.error)

        out: list[Table] = []
        schemas = self.config.get("schemas")
        if not schemas:
            try:
                default = self.inspector.default_schema_name
                schemas = [default] if default else [None]
            except Exception:  # noqa: BLE001
                schemas = [None]

        for schema in schemas:
            try:
                names = self.inspector.get_table_names(schema=schema)
                names += self.inspector.get_view_names(schema=schema)
            except Exception as exc:  # noqa: BLE001
                log.warning("listing tables in %s failed: %s", schema, exc)
                continue

            for name in names:
                try:
                    raw_columns = self.inspector.get_columns(name, schema=schema)
                    primary = set()
                    try:
                        pk = self.inspector.get_pk_constraint(name, schema=schema)
                        primary = set(pk.get("constrained_columns") or [])
                    except Exception:  # noqa: BLE001
                        pass
                except Exception as exc:  # noqa: BLE001
                    log.debug("columns for %s failed: %s", name, exc)
                    continue

                out.append(
                    Table(
                        name=name,
                        schema=schema or "",
                        columns=[
                            Column(
                                name=str(c["name"]),
                                type=str(c.get("type")),
                                nullable=bool(c.get("nullable", True)),
                                primary_key=str(c["name"]) in primary,
                                comment=str(c.get("comment") or ""),
                            )
                            for c in raw_columns
                        ],
                    )
                )
        return out

    # -- reading -----------------------------------------------------------

    def execute(self, sql: str, limit: int = 10_000, timeout: int = 60) -> QueryResult:
        assert_read_only(sql)
        if self.engine is None and not self.connect():
            raise ConnectorError(self.error)

        bounded = apply_limit(sql, limit, self.limit_syntax)
        started = time.time()

        from sqlalchemy import text

        with self.engine.connect() as conn:
            self._apply_timeout(conn, timeout)
            cursor = conn.execute(text(bounded))
            columns = list(cursor.keys())
            rows = [list(row) for row in cursor.fetchmany(limit + 1)]

        truncated = len(rows) > limit
        if truncated:
            rows = rows[:limit]

        return QueryResult(
            columns=[str(c) for c in columns],
            rows=rows,
            duration_ms=int((time.time() - started) * 1000),
            truncated=truncated,
            sql=bounded,
        )

    def _apply_timeout(self, conn: Any, timeout: int) -> None:
        """Best effort per-engine statement timeout."""
        return None


class PostgresConnector(SQLAlchemyConnector):
    kind = "postgresql"
    display_name = "PostgreSQL"
    dialect = "postgresql"
    driver_package = "psycopg2-binary"

    def _build_url(self) -> str:
        user, password, host, port, database = self._credentials()
        return f"postgresql+psycopg2://{user}:{password}@{host}:{port or 5432}/{database}"

    def _apply_timeout(self, conn: Any, timeout: int) -> None:
        conn.exec_driver_sql(f"SET statement_timeout = {int(timeout) * 1000}")


class MySQLConnector(SQLAlchemyConnector):
    kind = "mysql"
    display_name = "MySQL / MariaDB"
    dialect = "mysql"
    driver_package = "pymysql"

    def _build_url(self) -> str:
        user, password, host, port, database = self._credentials()
        return f"mysql+pymysql://{user}:{password}@{host}:{port or 3306}/{database}"

    def _apply_timeout(self, conn: Any, timeout: int) -> None:
        conn.exec_driver_sql(
            f"SET SESSION max_execution_time = {int(timeout) * 1000}"
        )


class MSSQLConnector(SQLAlchemyConnector):
    kind = "mssql"
    display_name = "SQL Server"
    dialect = "mssql"
    driver_package = "pyodbc"
    # SQL Server has no LIMIT; TOP goes after SELECT, so a generated query is
    # wrapped rather than suffixed.
    limit_syntax = "OFFSET 0 ROWS FETCH NEXT {limit} ROWS ONLY"

    def _build_url(self) -> str:
        user, password, host, port, database = self._credentials()
        driver = quote_plus(str(self.config.get("driver") or "ODBC Driver 18 for SQL Server"))
        return (
            f"mssql+pyodbc://{user}:{password}@{host}:{port or 1433}/{database}"
            f"?driver={driver}&TrustServerCertificate=yes"
        )


class SQLiteConnector(SQLAlchemyConnector):
    kind = "sqlite"
    display_name = "SQLite"
    dialect = "sqlite"

    def _build_url(self) -> str:
        path = self.config.get("path")
        if not path:
            raise ConnectorError("SQLite needs a file path")
        return f"sqlite:///{path}"


class DuckDBConnector(SQLAlchemyConnector):
    kind = "duckdb"
    display_name = "DuckDB"
    dialect = "duckdb"
    driver_package = "duckdb-engine"

    def _build_url(self) -> str:
        path = self.config.get("path") or ":memory:"
        return f"duckdb:///{path}"


class SnowflakeConnector(SQLAlchemyConnector):
    kind = "snowflake"
    display_name = "Snowflake"
    dialect = "snowflake"
    driver_package = "snowflake-sqlalchemy"

    def _build_url(self) -> str:
        user, password, _, _, database = self._credentials()
        account = self.config.get("account")
        if not account:
            raise ConnectorError("Snowflake needs an account identifier")
        parts = [f"snowflake://{user}:{password}@{account}/{database}"]
        schema = self.config.get("schema")
        if schema:
            parts.append(f"/{schema}")
        params = []
        for key in ("warehouse", "role"):
            value = self.config.get(key)
            if value:
                params.append(f"{key}={value}")
        if params:
            parts.append("?" + "&".join(params))
        return "".join(parts)

    def _apply_timeout(self, conn: Any, timeout: int) -> None:
        conn.exec_driver_sql(
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {int(timeout)}"
        )


class RedshiftConnector(SQLAlchemyConnector):
    kind = "redshift"
    display_name = "Amazon Redshift"
    dialect = "redshift"
    driver_package = "sqlalchemy-redshift"

    def _build_url(self) -> str:
        user, password, host, port, database = self._credentials()
        return f"redshift+psycopg2://{user}:{password}@{host}:{port or 5439}/{database}"


class BigQueryConnector(SQLAlchemyConnector):
    kind = "bigquery"
    display_name = "Google BigQuery"
    dialect = "bigquery"
    driver_package = "sqlalchemy-bigquery"

    def _build_url(self) -> str:
        project = self.config.get("project")
        if not project:
            raise ConnectorError("BigQuery needs a project id")
        dataset = self.config.get("dataset")
        return f"bigquery://{project}/{dataset}" if dataset else f"bigquery://{project}"


class ClickHouseConnector(SQLAlchemyConnector):
    kind = "clickhouse"
    display_name = "ClickHouse"
    dialect = "clickhouse"
    driver_package = "clickhouse-sqlalchemy"

    def _build_url(self) -> str:
        user, password, host, port, database = self._credentials()
        return f"clickhouse+http://{user}:{password}@{host}:{port or 8123}/{database}"


class TrinoConnector(SQLAlchemyConnector):
    kind = "trino"
    display_name = "Trino / Presto"
    dialect = "trino"
    driver_package = "trino"

    def _build_url(self) -> str:
        user, _, host, port, _ = self._credentials()
        catalog = self.config.get("catalog") or "hive"
        schema = self.config.get("schema") or "default"
        return f"trino://{user}@{host}:{port or 8080}/{catalog}/{schema}"


class OracleConnector(SQLAlchemyConnector):
    kind = "oracle"
    display_name = "Oracle"
    dialect = "oracle"
    driver_package = "oracledb"
    limit_syntax = "FETCH FIRST {limit} ROWS ONLY"

    def _build_url(self) -> str:
        user, password, host, port, database = self._credentials()
        return f"oracle+oracledb://{user}:{password}@{host}:{port or 1521}/?service_name={database}"


SQL_CONNECTORS: dict[str, type[SQLAlchemyConnector]] = {
    cls.kind: cls
    for cls in (
        PostgresConnector,
        MySQLConnector,
        MSSQLConnector,
        SQLiteConnector,
        DuckDBConnector,
        SnowflakeConnector,
        RedshiftConnector,
        BigQueryConnector,
        ClickHouseConnector,
        TrinoConnector,
        OracleConnector,
    )
}
