"""Tests for the optional GPU quantization path (small-VRAM laptops)."""

from app.main import _build_quantization_config, _quantization_mode


def _clear(monkeypatch):
    for var in ("QUANTIZATION", "LOAD_IN_4BIT", "LOAD_IN_8BIT"):
        monkeypatch.delenv(var, raising=False)


def test_quantization_disabled_by_default(monkeypatch):
    _clear(monkeypatch)
    assert _quantization_mode() is None


def test_quantization_4bit_via_flag(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LOAD_IN_4BIT", "1")
    assert _quantization_mode() == "4bit"


def test_quantization_8bit_via_explicit(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("QUANTIZATION", "8bit")
    assert _quantization_mode() == "8bit"


def test_explicit_none_overrides_flag(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LOAD_IN_4BIT", "1")
    monkeypatch.setenv("QUANTIZATION", "none")
    assert _quantization_mode() is None


def test_build_4bit_config_uses_nf4():
    config = _build_quantization_config("4bit")
    assert config.load_in_4bit is True
    assert config.bnb_4bit_quant_type == "nf4"
    assert config.bnb_4bit_use_double_quant is True


def test_build_unknown_config_returns_none():
    assert _build_quantization_config("weird") is None
