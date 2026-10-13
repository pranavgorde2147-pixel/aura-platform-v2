"""Validate the container build/deploy configuration without assuming a host
model checkpoint is present. The model mount itself is checked separately and
skipped when the checkpoint is unavailable (e.g. CI or non-GPU dev machines)."""

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = Path(os.getenv("MODEL_PATH", REPO_ROOT / "models" / "Qwen3.5-4B"))


@pytest.mark.skipif(not (MODEL_DIR / "config.json").exists(), reason="local model checkpoint not present")
def test_model_checkpoint_layout():
    assert MODEL_DIR.exists()
    assert (MODEL_DIR / "config.json").exists()


def test_no_model_load_requirement():
    assert "Qwen3.5-4B" in os.getenv("MODEL_NAME", "Qwen3.5-4B")


def test_dockerfile_includes_only_app_files():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "requirements.txt" in dockerfile
    assert "COPY app" in dockerfile
    assert "COPY tests" in dockerfile
    assert "COPY models" not in dockerfile
    assert "/models/" in dockerfile


def test_docker_compose_uses_read_only_model_mount():
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "8001:8001" in text
    assert "/models/Qwen3.5-4B:ro" in text
    assert "Qwen3.5-4B" in text
    assert "/home/pranav_gorde" not in text


def test_dockerignore_excludes_model_directory():
    dockerignore = (REPO_ROOT / ".dockerignore").read_text()
    assert "models/" in dockerignore
