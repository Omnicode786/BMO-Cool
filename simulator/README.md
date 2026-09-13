# Browser hardware simulator

This folder contains the browser UI only. It is **not a standalone fake**: when BMO is started with `--simulator`, the normal Python application replaces physical hardware adapters with browser-backed adapters while keeping the real EventBus, state machine, VAD, STT, brain, vision pipeline, game logic, mood system, TTS, and OLED renderer running.

## Quick start with local cloud mocks

```bash
bash scripts/run_simulator.sh
```

Open <http://127.0.0.1:8765>, enable the browser camera/microphone as desired, and interact with the controls.

## Run against configured Gemini / ElevenLabs services

```bash
. .venv/bin/activate
bmo-pi --simulator --debug
```

The browser sends:

- microphone PCM16 chunks -> `MicAudioChunk`
- webcam JPEG frames -> `FrameBuffer` + `CameraFrame`
- A/B/joystick actions -> `ControlEvent`
- TTP223 touch down/up -> push-to-talk events plus Python-timed tap/double/long gestures

Python sends back:

- the exact PIL image produced by `OLEDService`
- typed EventBus events (state, mode, mood, transcript, agent output, health, vision, etc.)
- TTS PCM for browser speaker playback

HTTP uses `--simulator-port` (default `8765`) and the WebSocket uses the next port (`8766`). The default host is loopback-only.

For the full webcam/microphone experience, open the simulator on the same machine as the Python process at `http://127.0.0.1:8765` (or use a secure HTTPS setup). Browsers generally restrict camera/microphone APIs on ordinary non-local HTTP pages, so opening `http://PI-IP:8765` from another computer may still allow controls/OLED but can block browser camera/mic permissions. If you bind with `--simulator-host 0.0.0.0`, do so only on a trusted network because the development simulator has no authentication.
