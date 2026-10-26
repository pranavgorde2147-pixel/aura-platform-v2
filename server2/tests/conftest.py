"""Shared test configuration.

Keep the suite independent of a developer's local ``server2/.env`` so that
model-name, skip-mode, and provider assertions stay deterministic. This runs
before ``app.main`` is imported, which is when the .env file would otherwise be
loaded.
"""

import os

import pytest

os.environ.setdefault("AURA_SKIP_DOTENV", "1")


@pytest.fixture(autouse=True)
def isolate_brain_vault_paths(tmp_path, monkeypatch):
    """Keep every test's private vaults and backups outside the repository."""
    monkeypatch.setenv("BRAIN_RESOURCE_ROOT", str(tmp_path / "brains"))
    monkeypatch.setenv("BRAIN_VAULT_BACKUP_ROOT", str(tmp_path / "brain_vault_backups"))
    from app import storage_config

    storage_config.get_settings.cache_clear()
    yield
    storage_config.get_settings.cache_clear()
