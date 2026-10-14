"""Shared test configuration.

Keep the suite independent of a developer's local ``server2/.env`` so that
model-name, skip-mode, and provider assertions stay deterministic. This runs
before ``app.main`` is imported, which is when the .env file would otherwise be
loaded.
"""

import os

os.environ.setdefault("AURA_SKIP_DOTENV", "1")
