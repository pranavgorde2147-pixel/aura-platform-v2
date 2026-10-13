"""CORS origin parsing accepts both comma-separated and JSON-array values."""

from app.core.config import Settings


def test_comma_separated_origins(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example, https://b.example")
    assert Settings().cors_allowed_origins == ["https://a.example", "https://b.example"]


def test_json_array_origins(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["https://a.example","https://b.example"]')
    assert Settings().cors_allowed_origins == ["https://a.example", "https://b.example"]


def test_empty_origins(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")
    assert Settings().cors_allowed_origins == []
