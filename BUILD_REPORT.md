# BMO Pi Build Report

Build date: **2026-09-06**  
Target: **Raspberry Pi 4, Raspberry Pi OS 64-bit Bookworm or newer (including Trixie), CPU-only baseline**  
Project version: **1.0.0**

## Result

The repository implements the requested realtime companion as a modular `asyncio` application. It keeps microphone capture, Gemini Live transcription, Gemini/ADK reasoning, ElevenLabs streaming TTS, speaker playback, local vision, OLED animation, physical controls, health monitoring, and memory in separate services joined by a bounded typed event bus.

The build host did **not** have Raspberry Pi camera/GPIO hardware or user API credentials. Hardware/cloud behavior is therefore covered by hardware-free unit/integration tests plus defensive runtime adapters and diagnostics; the final on-device checks listed below must be run on the Raspberry Pi.

## Architecture chosen

```text
16 kHz mono mic -> preprocessing/AEC -> 30 ms bounded PCM chunks
                                     -> persistent Gemini Live STT
                                     -> partial/final transcripts
                                     -> Gemini 3.7 Flash ADK agent
                                     -> streaming text deltas
                                     -> phrase/sentence chunker
                                     -> persistent ElevenLabs multi-context WS
                                     -> pcm_24000 chunks -> turn-safe playback

CSI camera -> low-res Picamera2 stream -> cheap motion gate -> MediaPipe hand tracking
                                                   |             -> temporal wave detector
                                                   |             -> local event policy
                                                   +-> one selected cloud frame ONLY for
                                                       a meaningful low-confidence candidate

Touch + Button A + Button B + 5-way joystick -> centralized controls/mode controller
OLED <- state/mood/game animator <- typed event bus -> health/memory/policy
```

### Priority and interruption rules

1. User speech has priority over proactive visual reactions.
2. A new speech turn receives a monotonically increasing `turn_id` immediately.
3. Barge-in invalidates the prior turn before output continues.
4. Speaker playback clears queued prior-turn PCM and checks `turn_id` again at write time.
5. The old ElevenLabs context is closed; a transport-loss context is never replayed on a new socket.
6. Gemini generation tasks check current turn ownership before emitting text.
7. Camera and MediaPipe work may drop stale frames; audio queues are bounded and never wait for vision.

## Current documentation/API verification

Official/current documentation was checked on 2026-09-06 before final packaging.

| Area | Current finding used by this project | Reference |
|---|---|---|
| Gemini brain | Stable model code is `gemini-3.7-flash`; function calling, structured outputs and multimodal input are supported; **Live API is not supported** by this model. | https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash |
| Gemini Live transcription | Dedicated realtime STT model is `gemini-3.5-transcribe-live`; current Google Gen AI SDK uses `client.aio.live.connect`; final + interim input transcription fields are documented. | https://ai.google.dev/gemini-api/docs/live-api/live-transcribe |
| Gemini Live VAD | Hybrid VAD supports local end-of-speech via `audio_stream_end`; manual push-to-talk supports `activity_start` / `activity_end`. | https://ai.google.dev/gemini-api/docs/live-api/live-transcribe |
| Gemini Live sessions | Current guidance includes context-window compression, session resumption handles, and `GoAway` handling for periodically renewed connections. | https://ai.google.dev/gemini-api/docs/live-api/session-management |
| Google ADK | `google-adk` stable package is 2.8.0; ADK recommends companion constraints files for transitive-dependency protection. | https://pypi.org/project/google-adk/ |
| Google Gen AI SDK | `google-genai` 2.19.0 is pinned as the ADK-compatible baseline used by this build. | https://pypi.org/project/google-genai/2.19.0/ |
| ElevenLabs TTS | Multi-context TTS WebSocket is `/v1/text-to-speech/{voice_id}/multi-stream-input`; contexts support `flush`, `close_context`, `close_socket`, and per-context audio. Current guide demonstrates `eleven_flash_v2_5`. | https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/multi-context-web-socket |
| Picamera2/libcamera | Picamera2 is the supported Python replacement for legacy Picamera; Bookworm+ camera tools are `rpicam-*`; Raspberry Pi recommends OS-package installation. | https://www.raspberrypi.com/documentation/computers/camera_software.html |
| Raspberry Pi GPIO/I2C | GPIO logic is 3.3 V; GPIO2/GPIO3 provide I2C SDA/SCL; I2C is enabled through `raspi-config`. | https://www.raspberrypi.com/documentation/computers/raspberry-pi.html and https://www.raspberrypi.com/documentation/computers/configuration.html |
| MediaPipe Tasks | Hand Landmarker `LIVE_STREAM` uses `detect_async`, callback results, monotonic timestamps, and may intentionally drop frames to lower latency. | https://ai.google.dev/edge/api/mediapipe/python/mp/tasks/vision/HandLandmarker |
| MediaPipe ARM64 packaging | `mediapipe` 1.0.1 currently publishes a `manylinux_2_28_aarch64` wheel, suitable for modern 64-bit Raspberry Pi OS glibc. | https://pypi.org/project/mediapipe/ |
| PipeWire AEC | Current native module is `libpipewire-module-echo-cancel`; the WebRTC engine is `aec/libspa-aec-webrtc`. | https://docs.pipewire.org/page_module_echo_cancel.html |

### Specification differences discovered

- **Gemini 3.7 Flash is not a Live model.** The implementation correctly keeps `gemini-3.5-transcribe-live` on the persistent realtime transcription connection and sends committed text to `gemini-3.7-flash` through ADK for reasoning/vision.
- The current Gemini Live transcription page now recommends roughly **100 ms** audio send chunks, while this project requirement explicitly demands approximately **20–40 ms**. This build intentionally uses **30 ms** raw PCM16 chunks at 16 kHz mono to satisfy the realtime design requirement. The data format remains the documented `audio/pcm;rate=16000`.
- Older MediaPipe Linux notes were commonly interpreted as excluding ARM64 Python wheels. Current PyPI packaging changed: MediaPipe 1.0.1 includes a Linux aarch64 wheel. No local source-build workaround is used.
- Raspberry Pi camera tooling is `rpicam-*` on current OS releases. Legacy `picamera`, `raspistill`, and `raspivid` are not used.

## Pinned package versions

Runtime packages in `requirements.txt` / `pyproject.toml`:

| Package | Pin | Reason |
|---|---:|---|
| google-adk | 2.8.0 | Current stable ADK release checked during build |
| google-genai | 2.19.0 | Compatible ADK-era Gen AI SDK baseline; contains current Live APIs used here |
| websockets | 15.0.1 | ADK-compatible WebSocket baseline and current client header API |
| pydantic | 2.12.5 | Typed config/tool schemas |
| PyYAML | 6.0.3 | YAML configuration |
| sounddevice | 0.5.6 | PortAudio raw PCM capture/playback |
| mediapipe | 1.0.1 | Current Tasks wheel with Linux aarch64 distribution |
| Pillow | 12.3.0 | OLED/image support |
| psutil | 7.2.2 | Runtime health metrics |
| gpiozero | 2.0.1.post3 | Centralized GPIO input handling |
| luma.oled | 3.15.0 | SSD1306 reference driver |
| pytesseract | 0.3.13 | Local OCR wrapper; `tesseract-ocr` itself is installed from apt |

Development pins: `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `ruff==0.16.6`.

Picamera2 and OpenCV are intentionally installed from Raspberry Pi OS apt (`python3-picamera2`, `python3-opencv`) and made visible to the project venv with `--system-site-packages`.

### Dependency-verification limitation

The build sandbox had no outbound package-download access, so a brand-new online `pip install` of Google ADK and all pins could not be executed here. The pins were cross-checked against current package metadata/documentation, the source is import-safe when optional cloud/Pi dependencies are absent, and `scripts/install.sh` performs a real installation followed by `pip check` on the Pi. This is the main environment-dependent verification item remaining.

## Automated verification run

Executed from the repository root after the final transport changes:

```text
python3 -m pytest -q
............................. [100%]
29 passed

python3 scripts/lint_project.py
Offline lint passed: 61 Python files parsed; no placeholders/trailing whitespace/tabs found.

python3 -m compileall -q src scripts tests
PASS

YAML parse: config/default.yaml, config/hardware.yaml
PASS

Placeholder scan for pass / TODO: implement
PASS
```

The final packaging verification additionally checks:

- required repository paths;
- YAML structure and production Pydantic config parsing;
- 30 ms realtime-audio constraint and reference 16 kHz mono hardware config;
- exact `.env.example` model defaults and empty secret values;
- Python syntax and production-module imports without Pi hardware;
- embedded-key patterns;
- systemd paths/restart/SIGTERM settings;
- README commands matching the actual layout;
- executable script modes.

## Unit/integration coverage

Automated tests include:

- temporal wave requires amplitude + multiple direction reversals;
- static raised hand is not a wave;
- uncertain wave becomes a cloud candidate rather than a confident local wave;
- motion persistence/debounce;
- global lighting jump rejection;
- event deduplication and cooldown;
- visual reaction policy does not preempt listening;
- legal and illegal state transitions;
- turn invalidation;
- stale audio rejection;
- streaming response text reaches TTS before Gemini generation finishes;
- barge-in closes old TTS and stale model text cannot resume it;
- ElevenLabs context creation reconnect/backoff and mid-context fail-closed behavior;
- TTS phrase/sentence segmentation;
- configuration and GPIO collision validation;
- high-confidence local vision path performs **zero** cloud requests;
- low-confidence path uploads **exactly one** representative frame;
- explicit `remember ...` persistence to human-readable JSONL + SQLite;
- memory-disabled mode writes no persistent memory files;
- hardware-free application composition/smoke import.

External APIs are mocked in automated integration tests. Tests do not require camera, GPIO, OLED, microphone, or speaker hardware.

## Requested acceptance scenarios

| Acceptance test | Architecture/test status | On-Pi action remaining |
|---|---|---|
| 1 — realtime voice | **Supported + integration-tested at event/TTS boundary.** Capture streams fixed 30 ms PCM chunks immediately; persistent Live STT commits text; ADK text deltas enter TTS before turn completion. | Confirm real microphone/device selection and measure network latency with real keys. |
| 2 — interruption | **Automated.** New `turn_id`, `BargeIn`, playback queue clear, stale PCM rejection, old ElevenLabs context close, stale Gemini chunks rejected. | Verify acoustic barge-in under actual speaker volume/AEC. |
| 3 — local wave | **Automated routing + temporal unit tests.** >=0.60 wave stays local and cloud request count remains zero. | Benchmark MediaPipe confidence with the actual camera/enclosure. |
| 4 — uncertain wave | **Automated.** One best frame from RAM ring buffer is passed once to the cloud classifier. | Validate Gemini vision with a real key/network. |
| 5 — tiny lighting changes | **Automated motion test.** Global-lighting jump rejection and temporal persistence prevent escalation. | Tune thresholds for the final room/camera exposure behavior. |
| 6 — read text | **Implemented.** Explicit camera tool uses local Tesseract OCR first and one Gemini vision frame only when OCR confidence is inadequate. | Confirm Tesseract quality/focus with the chosen camera. |
| 7 — camera failure | **Implemented failure isolation.** Camera is optional/retrying; voice stack is composed independently and can stay running. | Physically unplug/reconnect CSI/USB camera and observe OLED/status logs. |
| 8 — internet failure | **Implemented.** Health/network events switch state to offline; local camera/control/OLED services remain alive; cloud clients reconnect/fail safely. | Disconnect Wi-Fi/Ethernet and validate local PipeWire/device behavior. |

## Hardware-dependent items and assumptions

- Reference OLED: SSD1306 I2C 128x64 at bus 1 / `0x3C`. Driver is replaceable.
- Reference touch input: TTP223-style digital 3.3 V output on BCM GPIO17.
- Added controls: Button A BCM22, Button B BCM23, joystick up/down/left/right/press on BCM5/6/13/19/26. All are configurable and validated for collisions.
- Reference microphone: USB audio for the easiest build. There is no assumption that the Pi has a microphone jack.
- Reference speaker: powered USB/3.5 mm/USB audio or a passive speaker behind an appropriate amplifier. A bare speaker is never driven from GPIO.
- CSI camera is preferred; low-res local stream and higher-quality on-demand snapshots use Picamera2.
- AEC assumes a functioning per-user PipeWire/WirePlumber session and correct routing through the generated BMO echo-cancel sink/source. Fallback suppression exists but cannot match real WebRTC AEC.
- No AI accelerator is required. `lite` mode is intended for CPU-constrained Pi 4 configurations.
- No facial/identity recognition is implemented.

## Performance tuning notes

- Audio capture: 30 ms chunks; bounded queue 64; stale input drops oldest rather than accumulating an utterance.
- Playback: 20 ms slices; each slice is checked for current `turn_id` before writing.
- TTS: phrase boundaries 30–130 characters; persistent multi-context socket; `pcm_24000` direct playback.
- Normal camera profile: 640x360 @ 8 FPS; lite: 320x240 @ 5 FPS.
- MediaPipe inference is motion-gated and async live-stream; stale camera work may be dropped.
- Camera ring buffer defaults to 2.5 seconds in RAM only.
- Default local wave confidence threshold is 0.60 with a 20-second wave cooldown.
- OLED animation defaults to 8 FPS normal / 5 FPS lite.
- Health service tracks CPU, RAM, temperature, camera FPS, vision latency, and queue depths.
- If vision falls behind, camera frames are dropped. Audio is deliberately never made dependent on vision completion.

## Memory/personality additions

The user-requested “remember” behavior is implemented as opt-in explicit memory. Phrases matching the configured explicit remember trigger are stored as structured SQLite records and appended to `data/remembered_events.jsonl`, a human-readable history. Relevant memory search results are injected into later agent context. Raw microphone audio and camera images are not stored by this mechanism.

The companion is intentionally an **original retro game-console robot personality**, not a copyrighted dialogue clone. Mood state can shift among happy, curious, annoyed/angry, surprised/excited, proud, sleepy and related display expressions. The prompt permits light teasing, preferences, disagreement and occasional non-cooperation while prohibiting manipulative/abusive behavior. User safety/privacy instructions and hard hardware validation remain authoritative regardless of personality.

## Security/privacy checks

- `.env` is git-ignored; `.env.example` contains no secrets.
- No public web server or unauthenticated remote-control endpoint is included.
- Agent tools expose only typed, validated operations; no shell-execution tool or arbitrary GPIO access exists.
- Cloud-bound data is limited to live speech audio, committed transcript/context, generated response text to TTS, and a single selected image when vision escalation or an explicit image request requires it.
- Continuous camera frames, motion/hand processing, OLED, controls and event filtering are local.
- `SAVE_DEBUG_MEDIA=false` by default; production media paths do not persist raw mic/camera media.
- Rotating logging avoids unbounded log growth and redacts/never logs API key values.

## First on-device verification sequence

After copying/extracting the project on a Raspberry Pi:

```bash
cd bmo_pi
./scripts/install.sh
sudo /opt/bmo_pi/scripts/configure_pi.sh
sudoedit /opt/bmo_pi/.env
sudo -u bmo /opt/bmo_pi/.venv/bin/python /opt/bmo_pi/scripts/diagnose_hardware.py
sudo systemctl enable --now bmo.service
sudo systemctl status bmo.service
journalctl -u bmo.service -f
```

Do not enable the service until the diagnostic report shows the intended microphone/speaker paths and the `.env` contains your real Gemini key, ElevenLabs key, and licensed/custom voice ID.
