"""Explicit, retryable copies from the legacy shared DB into private vaults.

The migration deliberately never deletes or rewrites the source database.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brain_manager.db_models import (
    BrainConversation,
    BrainMessage,
    BrainProcessingJob,
    BrainVaultEmbedding,
    BrainVaultMigration,
    BrainVaultRecord,
    BrainVaultRelationship,
    BrainVectorSpace,
    StoredObject,
)
from app.brain_manager.registry.service import BrainRegistryService
from app.brain_manager.vault.service import BrainVaultService, chunk_text
from app.brain_manager.vault.storage import get_brain_vault_storage
from app.services.objects import checksum_and_size
from app.storage.base import get_brain_object_store, get_cached_object_store
from app.storage_db import ManagedBrain, ensure_schema, get_session_factory


_MIGRATED_MODELS = (
    BrainVaultRecord,
    BrainVaultRelationship,
    BrainVaultEmbedding,
    BrainVectorSpace,
    BrainConversation,
    BrainMessage,
    BrainProcessingJob,
)


@dataclass(frozen=True)
class VaultMigrationReport:
    brain_id: str
    owner_id: str
    status: str
    copied: dict[str, int]
    already_present: dict[str, int]
    ambiguous: dict[str, int]
    object_errors: int


async def _shared_tables(session: AsyncSession) -> set[str]:
    connection = await session.connection()
    return await connection.run_sync(
        lambda sync_connection: set(inspect(sync_connection).get_table_names())
    )


async def inventory_shared_vault(
    session: AsyncSession, brain: ManagedBrain
) -> dict[str, int]:
    """Count rows attached to the Brain without reading or returning content."""
    tables = await _shared_tables(session)
    inventory: dict[str, int] = {}
    for model in (*_MIGRATED_MODELS, StoredObject):
        if model.__tablename__ not in tables:
            inventory[model.__tablename__] = 0
            continue
        inventory[model.__tablename__] = int(
            await session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.brain_id == brain.brain_id)
            )
            or 0
        )
    return inventory


async def _migrate_shared_vault_impl(
    session: AsyncSession,
    brain: ManagedBrain,
) -> VaultMigrationReport:
    """Copy one Brain's legacy rows and object bytes, preserving the source."""
    manager = get_brain_vault_storage()
    await manager.ensure_vault(brain)
    state_result = await session.execute(
        select(BrainVaultMigration).where(
            BrainVaultMigration.brain_id == brain.brain_id
        )
    )
    state = state_result.scalars().first()
    if state is None:
        state = BrainVaultMigration(
            brain_id=brain.brain_id,
            owner_id=brain.owner_id,
            status="migrating",
        )
        session.add(state)
    else:
        state.status = "migrating"
        state.error_code = None
    await session.commit()

    copied = {model.__tablename__: 0 for model in (*_MIGRATED_MODELS, StoredObject)}
    present = {name: 0 for name in copied}
    ambiguous = {name: 0 for name in copied}
    object_errors = 0
    shared_tables = await _shared_tables(session)

    async with manager.session(brain) as private_session:
        for model in _MIGRATED_MODELS:
            if model.__tablename__ not in shared_tables:
                continue
            result = await session.execute(
                select(model).where(model.brain_id == brain.brain_id)
            )
            source_rows = list(result.scalars().all())
            for source in source_rows:
                if source.owner_id != brain.owner_id:
                    ambiguous[model.__tablename__] += 1
                    continue
                existing = await private_session.get(model, source.id) if hasattr(source, "id") else None
                if model is BrainVectorSpace:
                    existing = await private_session.get(model, brain.brain_id)
                if existing is not None:
                    present[model.__tablename__] += 1
                    continue
                values = {
                    column.key: getattr(source, column.key)
                    for column in model.__table__.columns
                }
                private_session.add(model(**values))
                copied[model.__tablename__] += 1
            orphan_count = await session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.brain_id.is_(None), model.owner_id == brain.owner_id)
            )
            ambiguous[model.__tablename__] += int(orphan_count or 0)
        await private_session.commit()

    object_model_present = "stored_objects" in shared_tables
    if object_model_present:
        result = await session.execute(
            select(StoredObject).where(StoredObject.brain_id == brain.brain_id)
        )
        legacy_objects = list(result.scalars().all())
        source_store = get_cached_object_store()
        destination_store = get_brain_object_store(brain)
        for source in legacy_objects:
            if source.owner_id != brain.owner_id:
                ambiguous["stored_objects"] += 1
                continue
            async with manager.session(brain) as private_session:
                existing = await private_session.get(StoredObject, source.id)
            if existing is not None:
                present["stored_objects"] += 1
                continue
            if not source_store.exists(source.object_key):
                object_errors += 1
                continue
            with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024) as stream:
                for chunk in source_store.open_stream(source.object_key):
                    stream.write(chunk)
                stream.seek(0)
                size_hash, size = checksum_and_size(stream)
                if size != source.size_bytes or size_hash != source.checksum_sha256:
                    object_errors += 1
                    continue
                stream.seek(0)
                destination_store.save(source.object_key, stream, size)
            async with manager.session(brain) as private_session:
                values = {
                    column.key: getattr(source, column.key)
                    for column in StoredObject.__table__.columns
                }
                private_session.add(StoredObject(**values))
                await private_session.commit()
            copied["stored_objects"] += 1
        orphan_objects = await session.scalar(
            select(func.count())
            .select_from(StoredObject)
            .where(StoredObject.brain_id.is_(None), StoredObject.owner_id == brain.owner_id)
        )
        ambiguous["stored_objects"] += int(orphan_objects or 0)

    missing_indexes = []
    async with manager.session(brain) as private_session:
        records_result = await private_session.execute(
            select(BrainVaultRecord).where(
                BrainVaultRecord.brain_id == brain.brain_id,
                BrainVaultRecord.owner_id == brain.owner_id,
                BrainVaultRecord.deleted_at.is_(None),
            )
        )
        for record in records_result.scalars().all():
            embedding_count = int(
                await private_session.scalar(
                    select(func.count(BrainVaultEmbedding.id))
                    .where(
                        BrainVaultEmbedding.brain_id == brain.brain_id,
                        BrainVaultEmbedding.record_id == record.id,
                    )
                )
                or 0
            )
            expected_chunks = len(chunk_text(record.content or record.title or ""))
            if expected_chunks and embedding_count < expected_chunks:
                missing_indexes.append(record)
    for record in missing_indexes:
        await BrainVaultService().index_record(brain, record)

    totals = await inventory_shared_vault(session, brain)
    migrated_id_checks: dict[str, bool] = {}
    for model in (*_MIGRATED_MODELS, StoredObject):
        if model.__tablename__ not in shared_tables:
            migrated_id_checks[model.__tablename__] = True
            continue
        source_key = next(iter(model.__table__.primary_key.columns))
        source_result = await session.execute(
            select(source_key).where(
                model.brain_id == brain.brain_id,
                model.owner_id == brain.owner_id,
            )
        )
        source_ids = set(source_result.scalars().all())
        async with manager.session(brain) as private_session:
            private_result = await private_session.execute(
                select(source_key).where(
                    model.brain_id == brain.brain_id,
                    model.owner_id == brain.owner_id,
                )
            )
            private_ids = set(private_result.scalars().all())
        migrated_id_checks[model.__tablename__] = source_ids.issubset(private_ids)

    missing_relationship_targets = 0
    missing_object_references = 0
    async with manager.session(brain) as private_session:
        record_ids = set(
            (
                await private_session.execute(
                    select(BrainVaultRecord.id).where(
                        BrainVaultRecord.brain_id == brain.brain_id,
                        BrainVaultRecord.owner_id == brain.owner_id,
                        BrainVaultRecord.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
        )
        relationships_result = await private_session.execute(
            select(BrainVaultRelationship.target_record_id).where(
                BrainVaultRelationship.brain_id == brain.brain_id,
                BrainVaultRelationship.owner_id == brain.owner_id,
                BrainVaultRelationship.target_record_id.is_not(None),
            )
        )
        missing_relationship_targets = sum(
            target_id not in record_ids
            for target_id in relationships_result.scalars().all()
        )
        object_keys = set(
            (
                await private_session.execute(
                    select(StoredObject.object_key).where(
                        StoredObject.brain_id == brain.brain_id,
                        StoredObject.owner_id == brain.owner_id,
                    )
                )
            ).scalars().all()
        )
        record_object_keys = (
            await private_session.execute(
                select(BrainVaultRecord.object_key).where(
                    BrainVaultRecord.brain_id == brain.brain_id,
                    BrainVaultRecord.owner_id == brain.owner_id,
                    BrainVaultRecord.object_key.is_not(None),
                )
            )
        ).scalars().all()
        missing_object_references = sum(
            object_key not in object_keys for object_key in record_object_keys
        )

    copied_records = copied["brain_vault_records"] + present["brain_vault_records"]
    record_count_verified = copied_records == totals["brain_vault_records"]
    references_verified = not missing_relationship_targets and not missing_object_references
    unresolved = (
        sum(ambiguous.values())
        + object_errors
        + sum(not verified for verified in migrated_id_checks.values())
        + missing_relationship_targets
        + missing_object_references
    )
    status = (
        "needs_review"
        if unresolved
        else "verified"
        if record_count_verified and references_verified
        else "failed"
    )

    state_result = await session.execute(
        select(BrainVaultMigration).where(
            BrainVaultMigration.brain_id == brain.brain_id
        )
    )
    state = state_result.scalars().first()
    assert state is not None
    state.owner_id = brain.owner_id
    state.status = status
    state.record_count = copied_records
    state.object_count = copied["stored_objects"] + present["stored_objects"]
    state.verification = {
        "source_inventory": totals,
        "copied": copied,
        "already_present": present,
        "ambiguous": ambiguous,
        "record_count_matches": record_count_verified,
        "source_ids_present_in_private_vault": migrated_id_checks,
        "relationships_verified": missing_relationship_targets == 0,
        "object_references_verified": missing_object_references == 0,
        "object_checksums_failed_or_missing": object_errors,
        "indexes_rebuilt": len(missing_indexes),
        "source_retained": True,
    }
    state.error_code = "ambiguous_or_missing_data" if unresolved else None
    await session.commit()

    return VaultMigrationReport(
        brain_id=str(brain.brain_id),
        owner_id=str(brain.owner_id),
        status=status,
        copied=copied,
        already_present=present,
        ambiguous=ambiguous,
        object_errors=object_errors,
    )


async def migrate_shared_vault(
    session: AsyncSession,
    brain: ManagedBrain,
) -> VaultMigrationReport:
    """Migrate a Brain and persist a retryable failure status if a step fails."""
    try:
        return await _migrate_shared_vault_impl(session, brain)
    except Exception as exc:
        await session.rollback()
        state_result = await session.execute(
            select(BrainVaultMigration).where(
                BrainVaultMigration.brain_id == brain.brain_id
            )
        )
        state = state_result.scalars().first()
        if state is not None:
            state.status = "failed"
            state.error_code = type(exc).__name__[:64]
            await session.commit()
        raise


async def _run_cli(brain_id: uuid.UUID, dry_run: bool) -> dict[str, Any]:
    await ensure_schema()
    async with get_session_factory()() as session:
        brain = await BrainRegistryService.get(session, brain_id)
        if brain is None:
            raise SystemExit(f"No registered Brain found for {brain_id}")
        if dry_run:
            return {
                "brain_id": str(brain.brain_id),
                "owner_id": str(brain.owner_id),
                "inventory": await inventory_shared_vault(session, brain),
                "source_retained": True,
            }
        return asdict(await migrate_shared_vault(session, brain))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy one Brain's legacy shared data into its private vault"
    )
    parser.add_argument("--brain-id", required=True, type=uuid.UUID)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(_run_cli(args.brain_id, args.dry_run))
    import json

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "VaultMigrationReport",
    "inventory_shared_vault",
    "migrate_shared_vault",
]
