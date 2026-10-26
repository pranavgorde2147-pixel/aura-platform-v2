"""Versioned, idempotent schema migrations for the Server 2 metadata database.

Server 2 does not use Alembic (it ships no migration dependency and its
metadata store is optional SQLite in development). Instead it keeps an
explicit ``schema_migrations`` table and an ordered list of migrations. Each
migration is written so that applying it to an up-to-date database is a no-op,
which makes the runner safe to call on every startup and safe to interrupt.

Rules the runner follows:

* never drop or rewrite user data;
* never guess a column type - the type is part of the migration;
* record what was applied so restarts are cheap;
* refuse to run when the database is at a *newer* version than this code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

#: Bump when a new migration is appended.
SCHEMA_VERSION = 2

_SCHEMA_TABLE = "schema_migrations"


@dataclass(frozen=True)
class ColumnSpec:
    """A column definition used by the migration operations."""

    name: str
    type: str  # "string" | "text" | "integer" | "boolean" | "json" | "datetime"
    nullable: bool = True
    server_default: str | None = None
    primary_key: bool = False
    unique: bool = False
    length: int | None = None


@dataclass(frozen=True)
class CreateTable:
    """Create a table when it does not exist yet."""

    name: str
    columns: Sequence[ColumnSpec]
    unique_constraints: Sequence[tuple[str, Sequence[str]]] = field(default_factory=tuple)
    indexes: Sequence[tuple[str, Sequence[str], bool]] = field(default_factory=tuple)


@dataclass(frozen=True)
class AddColumn:
    """Add a column when the table exists and the column does not."""

    table: str
    column: ColumnSpec


@dataclass(frozen=True)
class DropColumn:
    """Drop a column when it exists. Data in that column is lost by design."""

    table: str
    column: str


@dataclass(frozen=True)
class CreateIndex:
    """Create an index when it does not exist."""

    name: str
    table: str
    columns: Sequence[str]
    unique: bool = False


@dataclass(frozen=True)
class DropIndex:
    """Drop an index when it exists."""

    name: str
    table: str


@dataclass(frozen=True)
class CreateUniqueConstraint:
    """Add a UNIQUE constraint when the table exists and lacks it."""

    name: str
    table: str
    columns: Sequence[str]


@dataclass(frozen=True)
class ExecuteSQL:
    """Run a raw statement (used for data backfills)."""

    statement: str


Operation = (
    CreateTable
    | AddColumn
    | DropColumn
    | CreateIndex
    | DropIndex
    | CreateUniqueConstraint
    | ExecuteSQL
)


@dataclass(frozen=True)
class Migration:
    """One ordered schema migration."""

    version: int
    name: str
    operations: Sequence[Operation]


# ---------------------------------------------------------------------------
# Introspection helpers (dialect neutral)
# ---------------------------------------------------------------------------


async def _table_exists(conn: AsyncConnection, table: str) -> bool:
    result = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :name"
            if conn.dialect.name != "sqlite"
            else "SELECT 1 FROM sqlite_master WHERE type='table' AND name = :name"
        ),
        {"name": table},
    )
    return result.first() is not None


async def _column_names(conn: AsyncConnection, table: str) -> set[str]:
    if conn.dialect.name == "sqlite":
        result = await conn.execute(text(f'PRAGMA table_info("{table}")'))
        return {str(row[1]) for row in result.fetchall()}
    result = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :name"
        ),
        {"name": table},
    )
    return {str(row[0]) for row in result.fetchall()}


async def _index_names(conn: AsyncConnection, table: str) -> set[str]:
    if conn.dialect.name == "sqlite":
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = :name"),
            {"name": table},
        )
    else:
        result = await conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = :name"),
            {"name": table},
        )
    return {str(row[0]) for row in result.fetchall()}


def _column_ddl(column: ColumnSpec) -> str:
    if column.type == "string":
        base = f"VARCHAR({column.length or 255})"
    elif column.type == "text":
        base = "TEXT"
    elif column.type == "integer":
        base = "INTEGER"
    elif column.type == "boolean":
        base = "BOOLEAN"
    elif column.type == "json":
        base = "JSON"
    elif column.type == "datetime":
        base = "TIMESTAMP"
    else:  # pragma: no cover - guarded by the migration author
        raise ValueError(f"Unsupported column type: {column.type}")

    parts = [base]
    if column.primary_key:
        parts.append("PRIMARY KEY")
    elif not column.nullable:
        parts.append("NOT NULL")
    if column.unique and not column.primary_key:
        parts.append("UNIQUE")
    if column.server_default is not None:
        parts.append(f"DEFAULT {column.server_default}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Operation application
# ---------------------------------------------------------------------------


async def _apply_operation(conn: AsyncConnection, operation: Operation) -> None:
    if isinstance(operation, CreateTable):
        if await _table_exists(conn, operation.name):
            return
        columns = ", ".join(
            f'"{column.name}" {_column_ddl(column)}' for column in operation.columns
        )
        await conn.execute(text(f'CREATE TABLE "{operation.name}" ({columns})'))
        for constraint_name, constraint_columns in operation.unique_constraints:
            joined = ", ".join(f'"{item}"' for item in constraint_columns)
            await conn.execute(
                text(
                    f'CREATE UNIQUE INDEX "{constraint_name}" ON "{operation.name}" ({joined})'
                )
            )
        for index_name, index_columns, unique in operation.indexes:
            joined = ", ".join(f'"{item}"' for item in index_columns)
            keyword = "UNIQUE INDEX" if unique else "INDEX"
            await conn.execute(
                text(f'CREATE {keyword} "{index_name}" ON "{operation.name}" ({joined})')
            )
        return

    if isinstance(operation, AddColumn):
        if not await _table_exists(conn, operation.table):
            return
        existing = await _column_names(conn, operation.table)
        if operation.column.name in existing:
            return
        await conn.execute(
            text(
                f'ALTER TABLE "{operation.table}" ADD COLUMN "{operation.column.name}" '
                f"{_column_ddl(operation.column)}"
            )
        )
        return

    if isinstance(operation, DropColumn):
        if not await _table_exists(conn, operation.table):
            return
        existing = await _column_names(conn, operation.table)
        if operation.column not in existing:
            return
        await conn.execute(
            text(f'ALTER TABLE "{operation.table}" DROP COLUMN "{operation.column}"')
        )
        return

    if isinstance(operation, CreateIndex):
        if not await _table_exists(conn, operation.table):
            return
        existing = await _index_names(conn, operation.table)
        if operation.name in existing:
            return
        joined = ", ".join(f'"{item}"' for item in operation.columns)
        keyword = "UNIQUE INDEX" if operation.unique else "INDEX"
        await conn.execute(
            text(f'CREATE {keyword} "{operation.name}" ON "{operation.table}" ({joined})')
        )
        return

    if isinstance(operation, DropIndex):
        if not await _table_exists(conn, operation.table):
            return
        existing = await _index_names(conn, operation.table)
        if operation.name not in existing:
            return
        await conn.execute(text(f'DROP INDEX "{operation.name}"'))
        return

    if isinstance(operation, CreateUniqueConstraint):
        if not await _table_exists(conn, operation.table):
            return
        existing = await _index_names(conn, operation.table)
        if operation.name in existing:
            return
        joined = ", ".join(f'"{item}"' for item in operation.columns)
        await conn.execute(
            text(
                f'CREATE UNIQUE INDEX "{operation.name}" ON "{operation.table}" ({joined})'
            )
        )
        return

    if isinstance(operation, ExecuteSQL):
        await conn.execute(text(operation.statement))
        return

    raise TypeError(f"Unsupported migration operation: {type(operation)!r}")


# ---------------------------------------------------------------------------
# The migration list
# ---------------------------------------------------------------------------

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="brain_registry_resources",
        operations=(
            # --- managed_brains: the Brain registry -----------------------
            AddColumn(
                "managed_brains",
                ColumnSpec("llm_runtime_id", "string", nullable=True, length=64),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("llm_runtime_status", "string", nullable=False, server_default="'unregistered'"),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("runtime_isolation", "string", nullable=True, length=32),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("model_configuration_id", "string", nullable=True, length=128),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("memory_resource_reference", "string", nullable=True, length=255),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("vector_resource_reference", "string", nullable=True, length=255),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("vault_resource_reference", "string", nullable=True, length=255),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("resource_root", "string", nullable=True, length=512),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("manifest_version", "integer", nullable=False, server_default="1"),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("connected_app_count", "integer", nullable=False, server_default="0"),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("runtime_last_error", "string", nullable=True, length=500),
            ),
            AddColumn(
                "managed_brains",
                ColumnSpec("runtime_started_at", "datetime", nullable=True),
            ),
            # The legacy "shared_stateless" runtime status is replaced by the
            # explicit llm_runtime_status vocabulary. The old column is copied
            # first and dropped afterwards so no row loses its state.
            ExecuteSQL(
                "UPDATE managed_brains SET llm_runtime_status = 'unregistered' "
                "WHERE llm_runtime_status IS NULL"
            ),
            DropColumn("managed_brains", "runtime_status"),
            CreateIndex(
                "uq_managed_brains_llm_runtime_id",
                "managed_brains",
                ("llm_runtime_id",),
                unique=True,
            ),
            CreateIndex(
                "ix_managed_brains_llm_runtime_status",
                "managed_brains",
                ("llm_runtime_status",),
            ),
            # --- stored_objects: Brain-scoped vault documents ------------
            AddColumn("stored_objects", ColumnSpec("brain_id", "string", nullable=True, length=36)),
            CreateIndex("ix_stored_objects_brain_id", "stored_objects", ("brain_id",)),
        ),
    ),
    Migration(
        version=2,
        name="unified_brain_vault_namespaces",
        operations=(
            AddColumn(
                "brain_vault_records",
                ColumnSpec("namespace", "string", nullable=False, server_default="'shared'", length=160),
            ),
            AddColumn(
                "brain_vault_records",
                ColumnSpec("idempotency_key", "string", nullable=True, length=255),
            ),
            AddColumn(
                "brain_vault_records",
                ColumnSpec("version", "integer", nullable=False, server_default="1"),
            ),
            AddColumn(
                "brain_vault_records",
                ColumnSpec("deleted_at", "datetime", nullable=True),
            ),
            CreateIndex(
                "ix_brain_vault_records_namespace",
                "brain_vault_records",
                ("brain_id", "namespace"),
            ),
            CreateUniqueConstraint(
                "uq_brain_vault_records_idempotency",
                "brain_vault_records",
                ("brain_id", "idempotency_key"),
            ),
        ),
    ),
)


async def _ensure_schema_table(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER NOT NULL PRIMARY KEY, "
            "name VARCHAR(255) NOT NULL, "
            "applied_at TIMESTAMP NOT NULL"
            ")"
        )
    )


async def _applied_versions(conn: AsyncConnection) -> set[int]:
    await _ensure_schema_table(conn)
    result = await conn.execute(text("SELECT version FROM schema_migrations"))
    return {int(row[0]) for row in result.fetchall()}


async def run_migrations(conn: AsyncConnection) -> int:
    """Apply every pending migration. Returns the resulting schema version.

    Safe to call on every startup: applied migrations are skipped and each
    operation is itself idempotent.
    """
    applied = await _applied_versions(conn)
    newest = max(applied) if applied else 0
    if newest > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {newest} is newer than the supported version "
            f"{SCHEMA_VERSION}; deploy a matching Server 2 build"
        )

    for migration in sorted(MIGRATIONS, key=lambda item: item.version):
        if migration.version in applied:
            continue
        logger.info("Applying Server 2 schema migration %s: %s", migration.version, migration.name)
        for operation in migration.operations:
            await _apply_operation(conn, operation)
        await conn.execute(
            text(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (:version, :name, CURRENT_TIMESTAMP)"
            ),
            {"version": migration.version, "name": migration.name},
        )
        newest = max(newest, migration.version)
    return newest


__all__ = [
    "AddColumn",
    "ColumnSpec",
    "CreateIndex",
    "CreateTable",
    "CreateUniqueConstraint",
    "DropColumn",
    "DropIndex",
    "ExecuteSQL",
    "MIGRATIONS",
    "Migration",
    "SCHEMA_VERSION",
    "run_migrations",
]
