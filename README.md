# BMO Pi — realtime Raspberry Pi companion

BMO Pi is a production-oriented, modular Raspberry Pi 4 companion project with continuous streaming speech recognition, a Gemini 3.7 Flash ADK reasoning agent, low-latency ElevenLabs speech, local camera/gesture processing, an expressive OLED face, touch + joystick + buttons, a small local game, and opt-in local memory.

The companion is **inspired by the feel of a cheerful retro game-console robot**, but it is an original character named **Beemo**. It does not copy copyrighted dialogue, artwork, or a performer's voice. The TTS voice is always selected with your own `ELEVENLABS_VOICE_ID`, so use a voice you are licensed or authorized to use.

This repository was built against current documentation checked on **2026-09-06**. See `BUILD_REPORT.md` for the exact package/model choices and documentation differences discovered during the build.

## 1. What the architecture does

The realtime voice path is deliberately **not** “record a sentence → save WAV → upload → wait”. The microphone is opened continuously and produces 30 ms, 16 kHz, mono, signed PCM16 little-endian chunks. A small bounded queue feeds a persistent Gemini Live transcription session while local VAD supplies fast activity boundaries. A committed transcript starts a Gemini 3.7 Flash ADK turn. Gemini text deltas are grouped into natural phrase-sized chunks and fed to one reusable ElevenLabs multi-context WebSocket; PCM begins playing before the Gemini response is complete.

Barge-in has hard priority. New user speech invalidates the old turn ID, aborts speaker playback, clears queued PCM, closes the old ElevenLabs context, cancels the old Gemini generation task, and prevents stale callbacks from becoming audible.

The camera takes a different path: low-resolution frames stay local, pass through a cheap motion gate, then local person/hand processing. A confident temporal wave becomes structured JSON and **does not upload an image**. Only a meaningful but uncertain visual candidate can select exactly one representative frame from a bounded RAM ring and escalate it to Gemini vision.

See `docs/ARCHITECTURE.md`, `docs/AUDIO_PIPELINE.md`, and `docs/VISION_PIPELINE.md` for the detailed flows.

## 2. Why Gemini 3.7 and realtime transcription use different models

Current Google documentation separates these capabilities:

- `gemini-3.5-transcribe-live` is the current Gemini Live transcription model used for realtime audio transcription.
- `gemini-3.7-flash` is the stable reasoning/vision model used by the ADK agent, but it does **not** support the Gemini Live API.

Therefore this project keeps one persistent Live transcription connection for microphone audio and uses Gemini 3.7 Flash separately for reasoning, tools, and one-frame vision. It never tries to force `gemini-3.7-flash` into a Live session.

Official references:

- Gemini 3.7 Flash: <https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash>
- Gemini Live transcription: <https://ai.google.dev/gemini-api/docs/live-api/live-transcribe>
- Gemini Live session management: <https://ai.google.dev/gemini-api/docs/live-api/session-management>
- Google GenAI Python SDK: <https://googleapis.github.io/python-genai/>
- Google ADK: <https://google.github.io/adk-docs/>

## 3. Hardware

Baseline:

- Raspberry Pi 4 running Raspberry Pi OS 64-bit Bookworm or newer (including current Trixie releases)
- official/compatible CSI camera
- USB microphone (easiest first build) or supported I2S/USB audio interface
- powered USB / powered 3.5 mm / USB speaker, or a passive speaker **through an amplifier**
- SSD1306-compatible 128x64 I2C OLED (SH1106 is selectable)
- TTP223-style digital capacitive touch module
- two momentary push buttons
- five-way digital joystick module/switches
- reliable internet connection
- a good 5 V / 3 A Raspberry Pi 4 power supply; leave margin for USB/audio peripherals

A Raspberry Pi does not provide a normal microphone input jack. Do not connect a bare speaker to GPIO. GPIO signals are 3.3 V logic and **are not 5 V tolerant**.

## 4. Reference wiring

All GPIO numbers in `config/hardware.yaml` are **BCM numbers**.

| Device | Reference connection |
|---|---|
| CSI camera | Raspberry Pi CSI camera connector |
| OLED VCC | 3.3 V if your module supports it |
| OLED GND | GND |
| OLED SDA | GPIO2 / SDA1 |
| OLED SCL | GPIO3 / SCL1 |
| Touch VCC | 3.3 V |
| Touch GND | GND |
| Touch OUT | GPIO17 |
| Button A | GPIO22 to GND, internal pull-up |
| Button B | GPIO23 to GND, internal pull-up |
| Joystick up | GPIO5 to GND |
| Joystick down | GPIO6 to GND |
| Joystick left | GPIO13 to GND |
| Joystick right | GPIO19 to GND |
| Joystick press | GPIO26 to GND |

The Pi and all externally powered logic-level peripherals need a common ground where required by their interfaces. Verify the exact OLED/touch/joystick board voltage requirements before wiring; some breakout boards contain regulators or pull-ups that differ from bare devices.

For an enclosure, put the microphone and speaker as far apart as practical, avoid pointing the speaker directly at the microphone, mechanically isolate the mic from speaker vibration, and keep analog/audio wiring away from noisy DC/DC converters. See `docs/WIRING.md`.

## 5. Raspberry Pi OS installation

Copy/unzip the repository anywhere, then run:

```bash
cd bmo_pi
chmod +x scripts/*.sh scripts/*.py
./scripts/install.sh
```

The installer is idempotent and installs to `/opt/bmo_pi` by default. It:

- checks package availability before apt installation;
- installs Raspberry Pi OS Picamera2/OpenCV packages rather than random pip camera builds;
- creates a dedicated `bmo` service account;
- creates `.venv` with `--system-site-packages` so Picamera2/OpenCV remain visible;
- installs the pinned Python dependencies;
- creates `/opt/bmo_pi/.env` from `.env.example` if missing;
- downloads the official MediaPipe Hand Landmarker task model;
- installs `bmo.service`.

Then configure Pi interfaces:

```bash
sudo /opt/bmo_pi/scripts/configure_pi.sh
sudo reboot
```

If you prefer a different install path/user, set `BMO_INSTALL_DIR` and `BMO_SERVICE_USER` consistently and also update the reference systemd unit paths before installing it.

## 6. Enable/test I2C

The configuration script uses noninteractive `raspi-config` when available. Manually:

```bash
sudo raspi-config
# Interface Options -> I2C -> Enable
sudo reboot
```

After reboot:

```bash
i2cdetect -y 1
```

The reference SSD1306 address is `0x3C`. Some modules use `0x3D`; change `oled.i2c_address` in `config/hardware.yaml` if needed.

## 7. Camera test commands

Current Raspberry Pi OS uses `rpicam-*` commands:

```bash
rpicam-hello --list-cameras
rpicam-hello -t 5000
```

Some older Bookworm images may expose `libcamera-hello` instead. The application itself uses **Picamera2 + libcamera**, never legacy `picamera`.

## 8. Microphone test commands

List capture hardware:

```bash
arecord -l
arecord -L | less
```

Simple raw-hardware test example (choose the correct device for your machine):

```bash
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic.wav
aplay /tmp/mic.wav
```

For production, prefer the PipeWire echo-cancel virtual source described below. If automatic source matching does not select the right device, set `microphone.device` in `config/hardware.yaml` to the PortAudio device name/index.

## 9. Speaker test commands

List devices:

```bash
aplay -l
aplay -L | less
```

Then test through the chosen ALSA/PipeWire route with a known WAV. For a passive speaker, use a proper amplifier; never source speaker current from a GPIO pin.

## 10. Acoustic echo cancellation

The default `audio.aec_mode` is `pipewire`. `config/pipewire/echo-cancel.conf.example` uses current PipeWire 1.x `libpipewire-module-echo-cancel` syntax with the WebRTC AEC SPA library. `scripts/configure_pi.sh` installs it for the `bmo` service user.

After reboot, check:

```bash
sudo -u bmo XDG_RUNTIME_DIR=/run/user/$(id -u bmo) wpctl status
```

Look for **BMO Echo Cancel Source** and **BMO Echo Cancel Sink**. The capture/playback services automatically prefer device names matching `echo cancel source` / `echo cancel sink`. You can edit those hints in `config/default.yaml` if your PortAudio/PipeWire naming is different.

If PipeWire AEC is unavailable, set:

```yaml
audio:
  aec_mode: fallback
```

Fallback mode ducks speaker gain, raises the local VAD threshold while Beemo speaks, and suppresses microphone transmission during very loud likely-feedback frames. It preserves barge-in when possible but is not equivalent to real echo cancellation.

Official PipeWire module reference: <https://docs.pipewire.org/page_module_echo_cancel.html>

## 11. Configure `.env`

Edit only the installed secret file:

```bash
sudoedit /opt/bmo_pi/.env
sudo chmod 600 /opt/bmo_pi/.env
```

Minimum production values:

```dotenv
GEMINI_API_KEY=your_google_ai_key
ELEVENLABS_API_KEY=your_elevenlabs_key
ELEVENLABS_VOICE_ID=your_licensed_or_custom_voice_id

GEMINI_BRAIN_MODEL=gemini-3.7-flash
GEMINI_TRANSCRIBE_MODEL=gemini-3.5-transcribe-live
ELEVENLABS_MODEL_ID=eleven_flash_v2_5
ELEVENLABS_OUTPUT_FORMAT=pcm_24000
SAVE_DEBUG_MEDIA=false
LOG_LEVEL=INFO
BMO_PERFORMANCE_MODE=normal
```

Do not put real keys in `.env.example`; `.env` is git-ignored.

## 12. ElevenLabs voice ID

Set `ELEVENLABS_VOICE_ID` to a voice you are entitled to use. The default TTS model is `eleven_flash_v2_5` and normal playback requests raw `pcm_24000`, not MP3. The client keeps a multi-context WebSocket alive across turns, feeds phrase-sized text increments, explicitly flushes/ends a turn, and closes an interrupted context on barge-in.

Current ElevenLabs multi-context TTS docs: <https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/multi-context-web-socket>

## 13. Run interactively

Production hardware, foreground:

```bash
cd /opt/bmo_pi
sudo -u bmo -H env XDG_RUNTIME_DIR=/run/user/$(id -u bmo) .venv/bin/python -m bmo --debug
```

Hardware-free/mock development mode on a normal computer:

```bash
cd bmo_pi
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e . --no-deps
./scripts/run_dev.sh
```

`run_dev.sh` streams `tests/fixtures/user_question.wav` chunk-by-chunk, loops image fixtures, emulates the OLED in the console, accepts keyboard controls, and uses explicit cloud mocks. The mock switches are development-only; production code paths contain real provider implementations.

Keyboard controls in development: `a`/`b` buttons, `w/s/j/l` joystick directions, `p` joystick press, `t` touch tap, `d` double tap, `h` long press; type characters then Enter.

## 14. Run as a systemd service

```bash
sudo systemctl enable --now bmo.service
sudo systemctl status bmo.service
sudo journalctl -u bmo.service -f
```

Restart after config/key changes:

```bash
sudo systemctl restart bmo.service
```

Stop/disable:

```bash
sudo systemctl disable --now bmo.service
```

The unit runs as the non-root `bmo` user, restarts on unexpected failure, reads `/opt/bmo_pi/.env`, handles SIGTERM cleanly, and restricts writable paths to runtime data/log/model directories.

## 15. Hardware diagnostic

Run this **before** starting the service:

```bash
sudo -u bmo /opt/bmo_pi/.venv/bin/python /opt/bmo_pi/scripts/diagnose_hardware.py
```

It reports Python/architecture/RAM, CPU temperature, Pi throttling/undervoltage flags, camera detection, capture/playback devices, I2C/OLED address, configured touch/joystick/buttons, PipeWire/echo-cancel status, internet connectivity, model presence, and whether cloud credentials are configured. It never prints key contents.

Vision timing benchmark:

```bash
sudo -u bmo /opt/bmo_pi/.venv/bin/python /opt/bmo_pi/scripts/benchmark_vision.py
```

## 16. Tune motion and wave confidence

Edit `config/default.yaml`.

Important motion fields:

- `vision.motion.min_changed_area`: fraction of downscaled pixels that must change.
- `vision.motion.persistence_frames`: consecutive meaningful frames required.
- `vision.motion.lighting_change_*`: global lighting rejection.
- `vision.motion.exposure_reject_sec`: suppress camera auto-exposure transients.
- `vision.motion.cooldown_sec`: prevents repeated local work.

Wave fields:

- `window_sec`: temporal wrist-history window.
- `min_samples`: enough landmarks before a decision.
- `min_reversals`: required left/right direction reversals.
- `min_amplitude`: wrist travel relative to frame width.
- `raised_ratio`: fraction of samples where the wrist is sufficiently raised.
- `min_tracking_confidence`: landmark quality requirement.
- `local_confidence_threshold`: default `0.60`; this is **per-event model confidence, not prediction accuracy**.
- `cooldown_sec`: default 20 s to stop repeat greetings from one ongoing wave.

A raised hand alone is not classified as a wave.

## 17. OLED, touch, joystick, buttons, modes, and game

Hardware choices/pins live only in `config/hardware.yaml`. Set `oled.driver` to `ssd1306`, `sh1106`, or `console`. Touch and controls use GPIO Zero through the centralized hardware service—there is no arbitrary model-controlled GPIO tool.

Reference controls:

- **Touch single tap:** attention / happy local reaction; wakes from sleep.
- **Touch double tap:** immediately interrupts speech when Beemo is talking.
- **Touch long press:** sleep/wake.
- **Touch hold in `touch_to_talk`:** manual Gemini Live activity start/end boundaries.
- **Button A:** curious local reaction in companion mode; retry after game-over.
- **Button B:** cycle `companion -> game -> status -> sleep`.
- **Joystick press:** enter the game directly from companion mode.
- **Joystick left/right:** move in the local **Star Catcher** game.

The OLED has booting, idle, listening, thinking, speaking, happy, curious, annoyed, angry, surprised, sleepy, offline, and error expressions, blink/mouth animations, a live status dashboard, and a game renderer. Its FPS is capped and lower in lite mode so I2C animation cannot starve audio.

## 18. Personality, mood, and memory

The ADK system prompt defines an original compact robot companion: cheerful, curious, playful, occasionally stubborn, capable of gentle roasting, and emotionally expressive without being abusive or obnoxious. It is told that silence is often the correct reaction to routine background motion. Mood changes are generated locally from interaction events and decay over time; the OLED uses those mood states alongside the main state machine.

Explicit durable memory is local and simple. Saying a phrase such as:

> “Remember that my soldering iron is in drawer three.”

causes the deterministic conversation layer to save the fact before the LLM response is generated. It goes into SQLite and a human-readable `data/remembered_events.jsonl`. Later queries retrieve relevant local facts into the prompt and the model also has constrained `remember_fact` / `search_memories` tools. No raw microphone audio or camera images are stored by default.

To disable durable memory entirely:

```yaml
memory:
  enabled: false
```

When disabled, no memory database/log is created and remember-tool requests report that memory is disabled.

## 19. Privacy and what leaves the Pi

Normally **cloud-bound**:

- live 16 kHz speech PCM -> Gemini Live transcription;
- committed transcript + small structured context -> Gemini 3.7 Flash brain;
- one selected camera frame -> Gemini 3.7 vision only for explicit seeing tasks or eligible low-confidence local visual escalation;
- generated response text -> ElevenLabs TTS.

Normally **local-only**:

- continuous camera frames;
- motion and lighting/exposure gates;
- MediaPipe hand landmarks and temporal wave logic;
- local person detection;
- OCR-first text extraction;
- event filtering/deduplication/cooldowns;
- touch/buttons/joystick/game;
- OLED states/moods;
- structured memory database.

`SAVE_DEBUG_MEDIA=false` is the default and the production code does not persist raw microphone audio or camera frames. Recent camera frames live only in a bounded RAM ring. There is no facial-recognition/identity feature and no public web server or remote-control endpoint.

## 20. API usage and cost behavior

Cloud usage is intentionally bounded by architecture rather than by a hardcoded budget: microphone audio is continuously streamed while the selected listening mode is active; reasoning is one Gemini request per committed conversational turn or selected proactive event; visual frames are not continuously uploaded; ElevenLabs receives only generated response text. Provider pricing can change, so check current Google AI and ElevenLabs pricing pages for your account/region rather than relying on stale numbers in this repository.

To reduce CPU/cloud activity, set:

```dotenv
BMO_PERFORMANCE_MODE=lite
```

Lite mode reduces camera resolution/FPS, skips the OpenCV person detector, processes fewer motion frames with MediaPipe, and lowers OLED FPS. Audio remains the priority.

## 21. Common failure cases

**Brownouts / lightning-bolt / USB disconnects when volume rises:** use a stable 5 V / 3 A Pi 4 supply, power hungry amplifiers/USB speakers appropriately, and check `vcgencmd get_throttled`. Do not assume the Pi 5 V rail can supply arbitrary amplifier current.

**USB microphone disappears:** inspect `dmesg`, `arecord -l`, USB cable/hub power, and undervoltage flags. The capture supervisor retries device initialization rather than killing optional services.

**Noisy/feedback audio:** verify the BMO echo-cancel source/sink in `wpctl status`, increase physical mic/speaker separation, reduce enclosure vibration, lower speaker volume, and use `aec_mode: fallback` only when real PipeWire/WebRTC AEC cannot be used.

**Camera missing:** run `rpicam-hello --list-cameras`, reseat the CSI cable with power off, check connector orientation, and verify `camera_auto_detect=1`. Voice mode continues if the camera fails.

**OLED missing:** run `i2cdetect -y 1`, check `0x3C`/`0x3D`, 3.3 V/GND/SDA/SCL. Conversation continues headless if OLED initialization fails.

**Offline:** the OLED enters the offline face after repeated connectivity failures; local camera/touch/game/OLED continue, and cloud clients reconnect with bounded exponential backoff/jitter rather than crashing the whole app.

**Vision too slow:** run `scripts/benchmark_vision.py`, select `lite`, lower camera FPS/resolution, or increase `vision_every_n_motion_frames`. Camera queues are bounded/drop-old; they are never allowed to create unbounded lag that harms microphone responsiveness.

More detail: `docs/TROUBLESHOOTING.md`.

## Listening modes

Set in `config/default.yaml`:

```yaml
audio:
  listening_mode: vad  # always | vad | touch_to_talk
```

- `vad` (default): local energy VAD quickly detects start/end while Gemini Live server activity detection remains available; end-of-speech sends `audio_stream_end` without reconnecting the Live session.
- `always`: audio continuously flows and server activity detection owns turn boundaries.
- `touch_to_talk`: local touch press/release emits Gemini Live manual `activity_start` / `activity_end` signaling with automatic activity detection disabled for that session.

A future wake-word provider can be added before the VAD/STT gate without changing the rest of the event architecture.

## Tests and acceptance coverage

On a development machine:

```bash
python -m pytest
python scripts/lint_project.py
python -m compileall -q src tests scripts
```

The test suite mocks external APIs and needs no Pi hardware. It covers wave temporal logic, motion debounce/rejection, event cooldown/deduplication, state transitions, turn cancellation, stale audio rejection, TTS segmentation, configuration, local-vs-cloud visual routing, memory, and fixture-based realtime chunk flow.

The eight requested acceptance scenarios and exactly what is automated vs hardware/cloud dependent are recorded in `BUILD_REPORT.md`.

## Security notes

- `.env` is git-ignored and installed with mode 0600.
- Logs never intentionally print API key values.
- Agent tools validate arguments with Pydantic.
- There is no arbitrary shell-command tool.
- There is no arbitrary GPIO-number tool.
- There is no unauthenticated network server.
- The systemd service runs as non-root and uses systemd hardening.
- Use only authorized/licensed TTS voices.

## Repository map

```text
bmo_pi/
├── README.md                  # this guide
├── BUILD_REPORT.md            # verification + assumptions
├── config/                    # behavior, hardware, PipeWire AEC
├── docs/                      # architecture/audio/vision/wiring/troubleshooting
├── scripts/                   # install, configure, diagnostics, benchmark, dev
├── systemd/bmo.service
├── src/bmo/                   # modular application package
├── tests/                     # hardware-free unit/integration tests + fixtures
└── models/                    # install-time MediaPipe model
```

License: MIT for this project code. Third-party SDKs/models retain their own licenses/terms.
