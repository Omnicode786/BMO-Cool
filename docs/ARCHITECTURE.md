# Architecture

## Realtime dataflow

```text
                   +----------------------------------+
MIC 30 ms PCM ---->| Gemini Live Transcription        |
16 kHz mono S16LE  | persistent Live connection       |
  ^                | partial + committed transcript   |
  |                +----------------+-----------------+
  |                                 |
  |                        committed text
  |                                 v
  |                 +-------------------------------+
  |                 | Gemini 3.7 Flash ADK Agent    |
  |                 | tools + local memory context  |
  |                 +---------------+---------------+
  |                                 |
  |                         streaming text
  |                                 v
  |                 +-------------------------------+
  |                 | phrase-sized text chunker     |
  |                 +---------------+---------------+
  |                                 |
  |                                 v
  |                 +-------------------------------+
  |                 | ElevenLabs multi-context WS   |
  |                 | pcm_24000 output              |
  |                 +---------------+---------------+
  |                                 |
  |                         PCM audio chunks
  |                                 v
  |                              SPEAKER
  |                                 |
  +---------- PipeWire/WebRTC AEC <-+

CAMERA -> low-res frames -> motion gate -> local hand/person inference
                                      |
                    +-----------------+------------------+
                    |                                    |
             confidence >= .60                   .30 <= confidence < .60
                    |                                    |
             structured event JSON              ONE representative frame
                    |                                    |
                    +-----------> event policy     Gemini 3.7 vision
                                      |                    |
                                      +---------+----------+
                                                |
                                           ADK brain

TOUCH + BUTTONS + JOYSTICK ----------> typed EventBus <---------- OLED
                                              |
                                              +----> state / mood / game
```

The `.60` boundary is per-event confidence, not a claim about classifier accuracy. Accuracy is a dataset-level evaluation metric.

## Explicit state machine

`StateMachine` owns:

```text
BOOTING -> IDLE -> LISTENING -> THINKING -> SPEAKING -> IDLE
              \                                  /
               +------ SLEEPING / OFFLINE / ERROR
```

Services do not coordinate through scattered `isTalking`/`busy` flags. Typed state-change events are the shared source of truth.

## Turn ownership and barge-in

`TurnManager` monotonically increases integer turn IDs. User speech start creates/increments the current conversational turn. Every generated text chunk and every TTS/audio callback carries its turn ID.

Barge-in ordering:

1. local VAD notices new speech while state is `SPEAKING`;
2. `InterruptionService` invalidates the old turn and emits `BargeIn`;
3. playback aborts its output stream and clears old queued PCM;
4. ElevenLabs closes the interrupted context;
5. brain generation task is cancelled;
6. stale callbacks are ignored because `turns.is_current(turn_id)` is false;
7. the new user audio continues through the already-running Live STT session.

No stale output component is allowed to “win” after turn invalidation.

## Async services

The composition root starts consumers before producers. Principal services are:

- `AudioCaptureService` — PortAudio/sounddevice callback to bounded asyncio queue.
- `AudioPreprocessorService` — AEC fallback gating/speaker-aware attenuation logic.
- `VADService` — fast local activity boundaries and touch-to-talk boundaries.
- `GeminiLiveTranscriptionService` — persistent Live STT transport with resume/reconnect behavior.
- `BrainService` — transcript/visual priority, memory retrieval, streaming agent generation.
- `TTSService` — Gemini delta segmentation and turn-scoped ElevenLabs contexts.
- `AudioPlaybackService` — bounded PCM playback with stale-turn rejection.
- `CameraService` — Picamera2 low-res stream and explicit high-quality snapshots.
- `MotionService` — cheap grayscale frame gate.
- `LocalVisionService` — MediaPipe temporal hand tracking/wave detection + optional person detection.
- `CloudEscalationService` — exactly one selected frame for eligible uncertainty.
- `OLEDService` — non-blocking face/game/status animation.
- `GPIOControlsService` — sole owner of touch/buttons/joystick GPIO.
- `HealthService` — CPU/RAM/temp/throttling/queues/FPS/network monitor.

Blocking camera/vision/OCR work is moved out of the realtime async path using worker threads where needed. Queues have finite capacity.

## Priority rules

1. **New user speech / manual interrupt** — highest conversational priority.
2. Realtime microphone processing / speaker playback.
3. Committed speech turn reasoning/TTS.
4. Explicit user visual request (`what do you see?`, `read this`).
5. High-priority filtered visual events such as a clear wave.
6. Routine local vision work.
7. OLED animation/health telemetry.

Camera producers prefer dropping old frames over accumulating latency. A late frame is less useful than a current frame; audio chunks are treated differently because conversational audio continuity matters.

## Local/cloud boundaries

High-confidence local waves never need the cloud image. Low-confidence escalation is reachable only after a meaningful motion/hand candidate. Explicit visual tools capture a single current snapshot. No module exposes an arbitrary “stream camera to cloud” capability.

## Failure isolation

Optional peripheral failures publish `PeripheralFailed` and do not automatically stop unrelated capabilities. Camera loss yields voice-only mode; OLED loss yields headless conversation; touch loss leaves VAD voice interaction; network loss preserves local camera/control/game/OLED processing. Required core in-process coordination services may still fail the process so systemd can restart a genuinely broken application.
