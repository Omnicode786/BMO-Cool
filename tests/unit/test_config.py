"""Configuration parsing and hardware safety validation tests."""

from pathlib import Path

import pytest

from bmo.config import ElevenLabsConfig, HardwareConfig, load_settings


ROOT = Path(__file__).resolve().parents[2]


def test_project_yaml_parses() -> None:
    settings = load_settings(ROOT / "config", ROOT, env_file=ROOT / "tests" / "fixtures" / "empty.env")
    assert settings.behavior.audio.chunk_ms == 30
    assert settings.behavior.gemini.brain_model == "gemini-3.7-flash"
    assert settings.hardware.oled.i2c_address == 0x3C


def test_non_pcm_tts_output_is_rejected() -> None:
    with pytest.raises(ValueError):
        ElevenLabsConfig(output_format="mp3_44100_128")


def test_duplicate_gpio_assignment_is_rejected() -> None:
    with pytest.raises(ValueError):
        HardwareConfig.model_validate(
            {
                "microphone": {"sample_rate": 16000},
                "speaker": {"sample_rate": 24000},
                "touch": {"gpio_pin": 17},
                "button_a": {"gpio_pin": 17, "active_high": False},
            }
        )
