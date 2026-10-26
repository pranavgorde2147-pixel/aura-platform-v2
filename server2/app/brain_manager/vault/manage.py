"""Operator CLI for a single Brain vault's backup, restore and deletion."""

from __future__ import annotations

import argparse
import asyncio
import uuid

from app.brain_manager.registry.service import BrainRegistryService
from app.brain_manager.vault.storage import get_brain_vault_storage
from app.storage_db import ensure_schema, get_session_factory


async def _run(args: argparse.Namespace) -> str:
    await ensure_schema()
    async with get_session_factory()() as session:
        brain = await BrainRegistryService.get_owned(
            session, brain_id=args.brain_id, owner_id=args.owner_id
        )
        if brain is None:
            raise SystemExit("The Brain/owner pair does not match the Server 2 registry")

        storage = get_brain_vault_storage()
        if args.action == "backup":
            return str(await storage.backup(brain, args.backup_id))
        if args.action == "restore":
            await storage.restore(brain, args.backup_id)
            return "Vault restored from the selected verified backup"
        await storage.delete(brain, owner_id=args.owner_id, confirm=args.confirm_delete)
        return "Brain vault directory deleted; registry/runtime were not changed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore", "delete"))
    parser.add_argument("--brain-id", required=True, type=uuid.UUID)
    parser.add_argument("--owner-id", required=True, type=uuid.UUID)
    parser.add_argument("--backup-id", type=uuid.UUID)
    parser.add_argument("--confirm-delete", action="store_true")
    args = parser.parse_args()
    if args.action == "restore" and args.backup_id is None:
        parser.error("--backup-id is required for restore")
    if args.action == "delete" and not args.confirm_delete:
        parser.error("delete requires the explicit --confirm-delete flag")
    print(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
