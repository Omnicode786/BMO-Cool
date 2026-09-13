#!/usr/bin/env python3
"""Run packaging-oriented static and import checks without requiring Pi hardware."""

from __future__ import annotations

import ast
import importlib
import os
import pkgutil
import re
import stat
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

REQUIRED_PATHS = (
    "README.md",
    "BUILD_REPORT.md",
    "LICENSE",
    ".gitignore",
    ".env.example",
    "requirements.txt",
    "pyproject.toml",
    "config/default.yaml",
    "config/hardware.yaml",
    "config/pipewire/echo-cancel.conf.example",
    "scripts/install.sh",
    "scripts/configure_pi.sh",
    "scripts/diagnose_hardware.py",
    "scripts/benchmark_vision.py",
    "scripts/fetch_models.sh",
    "scripts/run_dev.sh",
    "systemd/bmo.service",
    "docs/ARCHITECTURE.md",
    "docs/AUDIO_PIPELINE.md",
    "docs/VISION_PIPELINE.md",
    "docs/WIRING.md",
    "docs/TROUBLESHOOTING.md",
    "docs/CONTROLS.md",
    "src/bmo/main.py",
    "tests/test_smoke.py",
)

SECRET_ENV_KEYS = ("GEMINI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID")
EXPECTED_ENV_DEFAULTS = {
    "GEMINI_BRAIN_MODEL": "gemini-3.7-flash",
    "GEMINI_TRANSCRIBE_MODEL": "gemini-3.5-transcribe-live",
    "ELEVENLABS_MODEL_ID": "eleven_flash_v2_5",
    "ELEVENLABS_OUTPUT_FORMAT": "pcm_24000",
}


def fail(message: str) -> None:
    raise SystemExit(f"VERIFY FAILED: {message}")


def parse_env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def verify_paths() -> None:
    missing = [path for path in REQUIRED_PATHS if not (ROOT / path).exists()]
    if missing:
        fail(f"missing required files: {', '.join(missing)}")
    if (ROOT / ".env").exists():
        fail("a real .env file is present in the source tree")


def verify_yaml_and_settings() -> None:
    for path in (ROOT / "config/default.yaml", ROOT / "config/hardware.yaml"):
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            fail(f"{path.relative_to(ROOT)} is not a YAML mapping")

    # Import the production validator and force secret variables empty for this check.
    from bmo.config import load_settings

    saved = {key: os.environ.get(key) for key in (*SECRET_ENV_KEYS, *EXPECTED_ENV_DEFAULTS)}
    try:
        for key in SECRET_ENV_KEYS:
            os.environ[key] = ""
        for key, value in EXPECTED_ENV_DEFAULTS.items():
            os.environ[key] = value
        settings = load_settings(ROOT / "config", ROOT, env_file=ROOT / "tests/fixtures/empty.env")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if settings.behavior.audio.chunk_ms not in range(20, 41):
        fail("audio.chunk_ms is outside the required 20-40 ms range")
    if settings.hardware.microphone.sample_rate != 16000 or settings.hardware.microphone.channels != 1:
        fail("reference microphone format is not mono 16 kHz")
    if settings.behavior.vision.wave.local_confidence_threshold != 0.60:
        fail("reference local wave confidence threshold is not 0.60")


def verify_env_example() -> None:
    values = parse_env_example()
    for key in SECRET_ENV_KEYS:
        if key not in values:
            fail(f".env.example is missing {key}")
        if values[key]:
            fail(f".env.example contains a non-empty secret value for {key}")
    for key, expected in EXPECTED_ENV_DEFAULTS.items():
        if values.get(key) != expected:
            fail(f".env.example {key} must be {expected!r}")


def verify_python_sources() -> None:
    files = sorted([*SRC.rglob("*.py"), *(ROOT / "scripts").glob("*.py"), *(ROOT / "tests").rglob("*.py")])
    for path in files:
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            fail(f"syntax error in {path.relative_to(ROOT)}: {exc}")
        if any(isinstance(node, ast.Pass) for node in ast.walk(tree)):
            fail(f"placeholder pass statement found in {path.relative_to(ROOT)}")
        implementation_marker = "TODO:" + " implement"
        if implementation_marker in source:
            fail(f"implementation placeholder found in {path.relative_to(ROOT)}")


def verify_imports() -> None:
    package = importlib.import_module("bmo")
    failures: list[str] = []
    for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        if module.name == "bmo.__main__":
            # Importing a conventional __main__ module intentionally executes the CLI.
            continue
        try:
            importlib.import_module(module.name)
        except Exception as exc:  # noqa: BLE001 - packaging report needs all import failures.
            failures.append(f"{module.name}: {type(exc).__name__}: {exc}")
    if failures:
        fail("module import failures: " + " | ".join(failures))


def verify_no_embedded_secrets() -> None:
    # Catch common real-key shapes without flagging documented variable names/placeholders.
    suspicious = (
        re.compile(r"AIza[0-9A-Za-z_-]{24,}"),
        re.compile(r"sk_[0-9A-Za-z_-]{24,}"),
    )
    text_extensions = {".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".sh", ".service", ".example", ""}
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".git", ".venv", ".pytest_cache", "__pycache__"} for part in path.parts):
            continue
        if path.suffix.lower() not in text_extensions and path.name not in {".gitignore", ".env.example"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(text) for pattern in suspicious):
            hits.append(str(path.relative_to(ROOT)))
    if hits:
        fail("possible API-key material found in: " + ", ".join(hits))


def verify_service_and_docs() -> None:
    unit = (ROOT / "systemd/bmo.service").read_text(encoding="utf-8")
    for required in (
        "WorkingDirectory=/opt/bmo_pi",
        "EnvironmentFile=/opt/bmo_pi/.env",
        "ExecStart=/opt/bmo_pi/.venv/bin/python -m bmo",
        "Restart=on-failure",
        "KillSignal=SIGTERM",
    ):
        if required not in unit:
            fail(f"systemd unit missing expected setting: {required}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in (
        "./scripts/install.sh",
        "sudo systemctl enable --now bmo.service",
        "/opt/bmo_pi/scripts/diagnose_hardware.py",
        "python -m pytest",
    ):
        if command not in readme:
            fail(f"README missing documented command: {command}")


def verify_script_modes() -> None:
    for path in (ROOT / "scripts").iterdir():
        if path.suffix not in {".sh", ".py"}:
            continue
        if not path.stat().st_mode & stat.S_IXUSR:
            fail(f"script is not executable: {path.relative_to(ROOT)}")


def main() -> int:
    verify_paths()
    verify_yaml_and_settings()
    verify_env_example()
    verify_python_sources()
    verify_imports()
    verify_no_embedded_secrets()
    verify_service_and_docs()
    verify_script_modes()
    print("Build verification passed: files, YAML/config, env defaults, imports, secrets, docs, service paths, and script modes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
