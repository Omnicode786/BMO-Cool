"""Basic project import/config smoke test requiring no Raspberry Pi peripherals."""

from pathlib import Path

import pytest

from bmo.config import load_settings
from bmo.main import build_parser


def test_smoke_import_and_parser() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = load_settings(root / "config", root, env_file=root / "tests" / "fixtures" / "empty.env")
    assert settings.behavior.audio.chunk_ms in range(20, 41)
    args = build_parser().parse_args(["--dev", "--mock-cloud"])
    assert args.dev and args.mock_cloud
    simulator_args = build_parser().parse_args(["--simulator", "--mock-cloud"])
    assert simulator_args.simulator and simulator_args.simulator_port == 8765


@pytest.mark.asyncio
async def test_application_composition_builds_in_hardware_free_mode(tmp_path: Path) -> None:
    from bmo.main import Application

    root = Path(__file__).resolve().parents[1]
    settings = load_settings(root / "config", tmp_path, env_file=root / "tests" / "fixtures" / "empty.env")
    args = build_parser().parse_args(
        [
            "--dev",
            "--mock-cloud",
            "--wav",
            str(root / "tests" / "fixtures" / "user_question.wav"),
            "--camera-fixtures",
            str(root / "tests" / "fixtures"),
            "--no-playback",
        ]
    )
    app = Application(settings, args)
    await app.build()
    services = {name: service for name, service, _required in app._services}
    assert services["brain"] is not None
    assert services["stt"] is not None
    assert services["tts"] is not None
    assert services["camera"] is not None


@pytest.mark.asyncio
async def test_application_composition_builds_with_browser_simulator(tmp_path: Path) -> None:
    from bmo.main import Application
    from bmo.simulator import (
        SimulatorAudioCaptureService,
        SimulatorBridgeService,
        SimulatorCameraService,
        SimulatorControlsService,
        SimulatorPlaybackService,
    )

    root = Path(__file__).resolve().parents[1]
    settings = load_settings(root / "config", tmp_path, env_file=root / "tests" / "fixtures" / "empty.env")
    args = build_parser().parse_args(["--simulator", "--mock-cloud"])
    app = Application(settings, args)
    await app.build()
    services = {name: service for name, service, _required in app._services}
    assert isinstance(services["simulator"], SimulatorBridgeService)
    assert isinstance(services["controls"], SimulatorControlsService)
    assert isinstance(services["camera"], SimulatorCameraService)
    assert isinstance(services["capture"], SimulatorAudioCaptureService)
    assert isinstance(services["playback"], SimulatorPlaybackService)
