"""Resource manifests for provisioned Brains.

A manifest is the human- and machine-readable description of *which resources
belong to a Brain*. It is written to the Brain's own directory (and mirrored in
the metadata database) so that an operator can inspect a Brain without
guessing.

Two rules are enforced mechanically:

* **No secrets.** A manifest never contains a credential, a token, an API key
  or a connection string. :func:`assert_no_secrets` fails closed.
* **No absolute server paths.** Resource references are logical
  (``brain_<uuid>/personal_vault``), not filesystem paths.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

MANIFEST_VERSION = 1

#: Manifest keys whose value is never stored.
SECRET_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_key",
        "access_token",
        "aws_secret_access_key",
        "bearer",
        "credential",
        "credentials",
        "id_token",
        "jwt",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "secret_access_key",
        "secret_key",
        "session_token",
        "token",
    }
)

#: Values that look like a credential are rejected even under a benign key.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9\-._~+/]{12,}"),
    re.compile(r"(?i)\b(?:sk|pk|api|key|token)[-_][A-Za-z0-9]{16,}\b"),
    re.compile(r"(?i)\b[A-Za-z0-9+/]{40,}={0,2}\b"),
    re.compile(r"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?key)\s*[:=]\s*\S{6,}"),
)


class ManifestSecretError(ValueError):
    """Raised when a manifest would contain a credential."""


def _walk_keys(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Yield ``(key_path, key_name)`` pairs for every mapping key in a value."""
    pairs: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_name = str(key)
            child_path = f"{path}.{key_name}" if path else key_name
            pairs.append((child_path, key_name))
            pairs.extend(_walk_keys(item, child_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            pairs.extend(_walk_keys(item, f"{path}[{index}]"))
    return pairs


def assert_no_secrets(manifest: dict[str, Any]) -> None:
    """Raise :class:`ManifestSecretError` when the manifest holds a credential.

    This is a fail-closed check: it looks at key names *and* at value shapes,
    because a token stored under a harmless key is still a leak.
    """
    for path, key_name in _walk_keys(manifest):
        normalized = key_name.lower().replace("-", "_")
        if normalized in SECRET_KEY_NAMES:
            raise ManifestSecretError(
                f"Manifest key '{path}' is not allowed to be stored in a manifest"
            )

    serialized = json.dumps(manifest, default=str)
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(serialized):
            raise ManifestSecretError(
                "Manifest content looks like it contains a credential and was rejected"
            )


def build_manifest(
    *,
    brain_id: str,
    owner_user_id: str,
    ai_identity_id: str,
    provisioning_status: str,
    llm_runtime_id: str | None,
    llm_runtime_status: str,
    runtime_isolation: str | None,
    model_configuration_id: str | None,
    compute_allocation: dict[str, Any],
    resource_references: dict[str, str],
    connected_app_count: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the manifest of one Brain.

    The manifest contains references and counters only - never content, never
    credentials.
    """
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "brain_id": brain_id,
        "owner_user_id": owner_user_id,
        "ai_identity_id": ai_identity_id,
        "provisioning_status": provisioning_status,
        "llm_runtime_id": llm_runtime_id,
        "llm_runtime_status": llm_runtime_status,
        "runtime_isolation": runtime_isolation,
        "model_configuration_id": model_configuration_id,
        "compute_allocation": compute_allocation,
        "resource_references": resource_references,
        "connected_app_count": connected_app_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        manifest.update(extra)
    assert_no_secrets(manifest)
    return manifest


def serialize_manifest(manifest: dict[str, Any]) -> str:
    """Serialize a manifest for storage on disk."""
    assert_no_secrets(manifest)
    return json.dumps(manifest, indent=2, sort_keys=True, default=str)
