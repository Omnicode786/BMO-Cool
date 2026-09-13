#!/usr/bin/env python3
"""Read-only Raspberry Pi hardware/cloud configuration diagnostic report."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402


def run(command: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        text = (proc.stdout or proc.stderr).strip()
        return proc.returncode, text
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def env_map() -> dict[str, str]:
    values = dict(os.environ)
    path = ROOT / ".env"
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip().strip("\"'") )
    return values


def read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def ram_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return 0
    return 0


def cpu_temp() -> str:
    paths = list(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    for path in paths:
        try:
            return f"{int(path.read_text().strip()) / 1000:.1f} C"
        except (OSError, ValueError):
            continue
    return "unavailable"


def throttled() -> tuple[str, list[str]]:
    if not shutil.which("vcgencmd"):
        return "unavailable", ["vcgencmd not found; install Raspberry Pi firmware utilities if desired."]
    code, out = run(["vcgencmd", "get_throttled"])
    if code != 0 or "0x" not in out:
        return out or "unavailable", ["Could not read throttling state."]
    try:
        value = int(out.split("0x", 1)[1], 16)
    except ValueError:
        return out, ["Unexpected vcgencmd output."]
    names = {
        0: "under-voltage now",
        1: "frequency capped now",
        2: "currently throttled",
        3: "soft temperature limit now",
        16: "under-voltage occurred since boot",
        17: "frequency capping occurred since boot",
        18: "throttling occurred since boot",
        19: "soft temperature limit occurred since boot",
    }
    active = [label for bit, label in names.items() if value & (1 << bit)]
    return f"0x{value:x}" + (" (clean)" if not active else " (" + ", ".join(active) + ")"), []


def camera_report() -> tuple[str, list[str]]:
    for command in (["rpicam-hello", "--list-cameras"], ["libcamera-hello", "--list-cameras"]):
        if shutil.which(command[0]):
            code, out = run(command, timeout=8)
            if code == 0 and ("Available cameras" in out or "0 :" in out or "0:" in out):
                return out.splitlines()[0:8].__str__(), []
            return out[:500] or "no camera reported", ["Check CSI ribbon orientation, connector latch, and camera_auto_detect=1."]
    return "camera listing utility not installed", ["Install rpicam-apps (or the OS camera utility package) and retest."]


def audio_report() -> tuple[str, str, list[str]]:
    suggestions: list[str] = []
    mic = "unavailable"
    speaker = "unavailable"
    if shutil.which("arecord"):
        _, out = run(["arecord", "-l"])
        mic = "\n".join(out.splitlines()[:12]) or "no capture devices listed"
    else:
        suggestions.append("Install alsa-utils for arecord/aplay diagnostics.")
    if shutil.which("aplay"):
        _, out = run(["aplay", "-l"])
        speaker = "\n".join(out.splitlines()[:12]) or "no playback devices listed"
    return mic, speaker, suggestions


def i2c_report(bus: int, expected: int) -> tuple[str, list[str]]:
    dev = Path(f"/dev/i2c-{bus}")
    if not dev.exists():
        return f"{dev} missing", ["Enable I2C with raspi-config, then reboot."]
    if not shutil.which("i2cdetect"):
        return f"{dev} exists, i2cdetect missing", ["Install i2c-tools."]
    code, out = run(["i2cdetect", "-y", str(bus)])
    needle = f"{expected:02x}"
    found = code == 0 and needle in out.lower().split()
    return f"expected OLED 0x{expected:02X}: {'FOUND' if found else 'not found'}\n{out}", ([] if found else ["Check OLED power/GND/SDA/SCL and its actual 0x3C/0x3D address."])


def pipewire_report(hint: str) -> tuple[str, list[str]]:
    parts: list[str] = []
    suggestions: list[str] = []
    if shutil.which("wpctl"):
        code, out = run(["wpctl", "status"])
        parts.append("wpctl: " + ("ok" if code == 0 else "failed"))
        parts.append("\n".join(out.splitlines()[:28]))
    else:
        suggestions.append("PipeWire wpctl not found; install wireplumber/pipewire tools.")
    source_text = ""
    if shutil.which("pactl"):
        _, source_text = run(["pactl", "list", "short", "sources"])
        parts.append("sources:\n" + source_text[:1000])
    if hint.lower() not in source_text.lower():
        suggestions.append("Echo-cancel virtual source not detected; install the provided PipeWire drop-in and restart/reboot.")
    return "\n".join(parts) or "PipeWire tools unavailable", suggestions


def internet_report(host: str, port: int) -> tuple[str, list[str]]:
    try:
        with socket.create_connection((host, port), timeout=4):
            return f"TCP {host}:{port} reachable", []
    except OSError as exc:
        return f"unreachable: {exc}", ["Check DNS/Wi-Fi/Ethernet; local camera/touch/OLED should still work offline."]


def print_section(title: str, value: str, suggestions: list[str] | None = None) -> None:
    print(f"\n=== {title} ===")
    print(value)
    for suggestion in suggestions or []:
        print(f"SUGGESTION: {suggestion}")


def main() -> int:
    hardware = read_yaml(ROOT / "config" / "hardware.yaml")
    behavior = read_yaml(ROOT / "config" / "default.yaml")
    env = env_map()

    print("BMO Pi hardware diagnostic (read-only; secrets are never printed)")
    print_section("Python", f"{sys.version.split()[0]} ({sys.executable})")
    print_section("Architecture", f"{platform.machine()} / {platform.platform()}")
    print_section("RAM", f"{ram_mb()} MiB total")
    print_section("CPU temperature", cpu_temp())
    throttle, tips = throttled()
    print_section("Power/throttling", throttle, tips)

    camera, tips = camera_report()
    print_section("Camera", camera, tips)
    mic, speaker, tips = audio_report()
    print_section("Microphone devices", mic, tips)
    print_section("Speaker devices", speaker)

    oled = hardware.get("oled", {})
    i2c, tips = i2c_report(int(oled.get("i2c_bus", 1)), int(oled.get("i2c_address", 0x3C)))
    print_section("I2C / OLED", i2c, tips)
    touch = hardware.get("touch", {})
    buttons = {name: hardware.get(name, {}) for name in ("button_a", "button_b")}
    joystick = hardware.get("joystick", {})
    control_text = [
        f"touch: enabled={touch.get('enabled')} GPIO{touch.get('gpio_pin')} active_high={touch.get('active_high')}",
        *[f"{name}: enabled={cfg.get('enabled')} GPIO{cfg.get('gpio_pin')}" for name, cfg in buttons.items()],
        "joystick: " + ", ".join(f"{k}={v}" for k, v in joystick.items() if k.endswith("_pin")),
        f"gpiochip present: {Path('/dev/gpiochip0').exists()}",
    ]
    print_section("Touch/buttons/joystick config", "\n".join(control_text), ["All GPIO inputs must stay within Raspberry Pi 3.3 V logic levels."])

    aec_hint = behavior.get("audio", {}).get("echo_cancel_source_hint", "echo-cancel")
    pw, tips = pipewire_report(str(aec_hint))
    print_section("PipeWire / echo cancellation", pw, tips)

    host = behavior.get("health", {}).get("internet_host", "generativelanguage.googleapis.com")
    port = int(behavior.get("health", {}).get("internet_port", 443))
    internet, tips = internet_report(str(host), port)
    print_section("Internet", internet, tips)

    gemini_ok = bool(env.get("GEMINI_API_KEY", "").strip())
    eleven_ok = bool(env.get("ELEVENLABS_API_KEY", "").strip() and env.get("ELEVENLABS_VOICE_ID", "").strip())
    print_section("Gemini API configuration", "configured" if gemini_ok else "MISSING", [] if gemini_ok else ["Set GEMINI_API_KEY in /opt/bmo_pi/.env."])
    print_section("ElevenLabs API configuration", "key + voice id configured" if eleven_ok else "MISSING key and/or voice id", [] if eleven_ok else ["Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in .env."])

    model = ROOT / behavior.get("vision", {}).get("hand_landmarker_model", "models/hand_landmarker.task")
    print_section("MediaPipe model", f"{model}: {'present' if model.is_file() and model.stat().st_size > 0 else 'MISSING'}", [] if model.is_file() else ["Run scripts/fetch_models.sh."])
    print("\nDiagnostic complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
