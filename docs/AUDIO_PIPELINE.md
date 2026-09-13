# Audio pipeline

## Capture format and chunking

The normal microphone format is mono signed 16-bit little-endian PCM at 16 kHz. `audio.chunk_ms` is validated to 20–40 ms and defaults to 30 ms (480 samples / 960 bytes at 16 kHz mono PCM16). `AudioCaptureService` never builds a complete utterance and does not create normal conversational WAV files.

```text
PortAudio callback -> bounded MicAudioChunk queue -> preprocessing -> VAD/STT
```

The callback crosses into asyncio with thread-safe scheduling. If the capture path falls behind, its queue is bounded; it cannot consume memory indefinitely.

## Gemini Live transcription

`GeminiLiveTranscriptionService` uses the current Google GenAI Python SDK async Live API with `gemini-3.5-transcribe-live`. It keeps a session alive across microphone sentences.

The service handles:

- realtime PCM `Blob` sends;
- partial input transcription;
- committed/final input transcription;
- server activity detection in `vad`/`always` mode;
- local-VAD `audio_stream_end` hints in `vad` mode;
- manual `ActivityStart`/`ActivityEnd` in `touch_to_talk` mode;
- session-resumption handles where supplied;
- GoAway/session turnover;
- context compression when configured;
- reconnect with exponential backoff and jitter.

Changing listening mode causes a controlled session reconfiguration, not one reconnect per sentence.

## AEC

Best path:

```text
physical mic -> PipeWire WebRTC echo cancel -> BMO Echo Cancel Source -> app
app -> BMO Echo Cancel Sink -> echo reference + physical speaker
```

The PipeWire drop-in is `config/pipewire/echo-cancel.conf.example`. Current PipeWire echo-cancel uses the `libpipewire-module-echo-cancel` module and the WebRTC SPA AEC implementation (`aec/libspa-aec-webrtc`).

Fallback mode is intentionally conservative:

- speaker gain is ducked;
- VAD threshold rises while speaking;
- very loud microphone frames likely dominated by speaker bleed may be marked `transmit=false`;
- true local speech above the raised threshold can still trigger barge-in.

Fallback cannot match a correctly configured echo canceller. Physical acoustic design still matters.

## VAD and listening modes

### `vad`

Default. Local RMS/energy VAD detects speech quickly. It requires several start/end frames to reject spikes. While Beemo speaks, the threshold is multiplied by `vad_threshold_while_speaking_multiplier` to reduce self-triggering. The Google Live server still performs speech-start activity detection; local end-of-speech sends `audio_stream_end` for low latency.

### `always`

Processed chunks continue to the Live session and server activity detection determines boundaries. Useful for testing; potentially more cloud audio usage.

### `touch_to_talk`

Touch press creates `PushToTalkStarted` and a turn; release creates `PushToTalkEnded`. The Live session is configured with automatic activity detection disabled and receives explicit manual activity messages. The same touch sensor still supports tap/double/long UI behavior when not used as a hold.

## Streaming brain -> TTS

Gemini output is not accumulated into a full reply before speech starts:

```text
ADK text delta -> TextChunker -> phrase -> ElevenLabs context -> PCM -> playback
```

`TextChunker` favors punctuation/word boundaries and enforces min/max chunk lengths. It avoids isolated-token TTS calls while preserving low latency.

The ElevenLabs client uses the multi-stream/multi-context WebSocket endpoint. One connection can serve successive turn contexts. Each turn initializes a context, feeds text, explicitly flushes at generation end, then closes the context. `auto_mode` defaults to false because the application sends incremental phrases rather than guaranteed complete sentences; it is configurable.

## Playback and stale-data safety

`AudioPlaybackService` splits provider chunks into small playback slices, checks the turn before writing, and records the speaker-first-audio timestamp. A barge-in aborts the stream and empties the queue. Enqueued old-turn chunks are rejected again on dequeue, so callbacks that arrive late cannot resume the prior answer.

## Latency markers

Per turn, `LatencyTracker` records:

- `speech_start`
- `speech_end`
- `stt_commit`
- `gemini_request_start`
- `gemini_first_token`
- `elevenlabs_first_audio`
- `speaker_first_audio`

Derived durations are emitted through structured logs. Network/voice choice/API load dominate actual end-to-first-audio latency; the architecture avoids full-utterance and full-response buffering that would add unnecessary delay.
