"""Validate the container build/deploy configuration without assuming a host
model checkpoint is present. The model mount itself is checked separately and
skipped when the checkpoint is unavailable (e.g. CI or non-GPU dev machines)."""

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_FILE = Path(os.getenv("MODEL_PATH", REPO_ROOT / "models" / "Qwen3.5-0.8B-Q4_K_M.gguf"))


@pytest.mark.skipif(not MODEL_FILE.exists(), reason="local model checkpoint not present")
def test_model_checkpoint_layout():
    assert MODEL_FILE.exists()
    assert MODEL_FILE.suffix == ".gguf" or MODEL_FILE.is_dir()


def test_default_model_name():
    assert "Qwen3.5" in os.getenv("MODEL_NAME", "Qwen3.5-0.8B")


def test_dockerfile_includes_only_app_files():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "requirements.txt" in dockerfile
    assert "COPY app" in dockerfile
    assert "COPY tests" in dockerfile
    assert "COPY models" not in dockerfile
    assert "/models/" in dockerfile
    assert "/var/data" in dockerfile


def test_docker_compose_uses_read_only_model_mount():
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "8001:8001" in text
    assert "/models:ro" in text
    assert "Qwen3.5-0.8B" in text
    assert "STORAGE_BACKEND" in text
    assert "/var/data" in text
    assert "/home/pranav_gorde" not in text


def test_dockerignore_excludes_model_directory():
    dockerignore = (REPO_ROOT / ".dockerignore").read_text()
    assert "models/" in dockerignore
