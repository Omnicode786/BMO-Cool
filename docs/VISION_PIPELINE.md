# Vision pipeline

## Goals

Continuous video is processed locally. Cloud vision is an escalation path, not the default camera path.

```text
Picamera2 low-res stream (5–8 FPS typical)
        |
        v
cheap grayscale motion gate
        |
 meaningful, persistent motion only
        v
MediaPipe hand landmark LIVE_STREAM + optional OpenCV person detector
        |
        +-- temporal wave confidence >= 0.60 --> local VisualEvent JSON
        |
        +-- meaningful candidate 0.30..0.60 --> choose ONE sharp frame --> Gemini vision
```

Explicit “what do you see?” and “read this” tools bypass normal motion thresholds but still use one snapshot, not video upload.

## Camera

`CameraService` uses Picamera2/libcamera. The normal configuration is low resolution for local processing; `capture_snapshot()` temporarily uses the higher snapshot resolution for an explicit cloud/vision task. Recent frames live in `FrameBuffer`, a bounded RAM ring.

If camera capture fails, the service publishes a peripheral fault and retries; voice services do not depend on the camera object being healthy.

## Motion gate

`MotionDetector` downsizes and grayscales frames, smooths them, and compares against a recent background/previous frame. It includes:

- absolute pixel-delta threshold;
- minimum changed-area fraction;
- temporal persistence;
- debounce;
- cooldown;
- mean-brightness/global-change rejection;
- short auto-exposure transient rejection.

One changed pixel, a brief exposure jump, or a tiny curtain movement does not qualify for expensive local inference or cloud escalation.

## MediaPipe hand tracking

`MediaPipeHandTracker` uses the MediaPipe Tasks Hand Landmarker in `LIVE_STREAM` mode. `detect_async()` is appropriate for streaming because the task runner may drop stale inference frames rather than queueing latency. The application also avoids backlogging camera frames.

The install script downloads the official Google-hosted float16 Hand Landmarker task model.

Current MediaPipe 1.0.1 publishes a Linux `manylinux_2_28_aarch64` wheel, so a 64-bit Raspberry Pi OS Bookworm baseline can install the pinned wheel. This is called out in `BUILD_REPORT.md` because older repository guidance about Linux aarch64 wheel availability can be stale.

## Temporal wave detection

A wave is not a static hand label. `WaveDetector` stores a short history of normalized wrist positions and computes evidence from:

- landmark tracking confidence;
- number of valid samples;
- fraction of samples with a sufficiently raised wrist;
- horizontal travel amplitude;
- multiple left/right direction reversals;
- temporal consistency/window duration;
- person-presence evidence.

The result is a combined confidence. Once a wave passes the local threshold, the detector clears/holds its history and event policy adds a longer cooldown so one continuing wave does not trigger repeated greetings.

Example local output:

```json
{
  "type": "wave",
  "confidence": 0.86,
  "source": "local_vision",
  "person_present": true,
  "summary": "A person appears to be waving toward the device."
}
```

No frame accompanies this high-confidence event to the cloud.

## Low-confidence escalation

`CloudEscalationService` receives only eligible `LowConfidenceVisualCandidate` events. It asks `FrameBuffer` for the best representative frame around the candidate, prioritizing useful sharpness, resizes to a bounded width, JPEG-compresses, and sends **one** image to `GeminiVisionClient`.

Gemini is requested to return a validated structured schema with:

- event type;
- description;
- numeric confidence;
- whether a companion reaction is appropriate;
- suggested conversational context.

The Pydantic model rejects malformed model output. Low model confidence is preserved; the application does not manufacture certainty.

## OCR is separate

`TesseractOCR` exists for text reading, not scene understanding. `read_visible_text()` captures one frame and tries local OCR first. If extracted text confidence passes `vision.ocr_confidence_threshold`, only text is returned to the agent. If OCR is weak and Gemini vision is configured, a single frame can be escalated with a text-reading instruction.

OCR triggers are intended for explicit “read this” requests, likely text tasks, or the constrained agent tool. It is not used to infer arbitrary activity.

## Event policy

`EventPolicy` assigns priority, deduplicates repeated semantic events, applies reaction cooldowns, and checks conversational state. Routine visual chatter is deliberately suppressed while the user is speaking/listening. Typical clear waves/object presentations can be proactive; small scene changes normally remain silent.

No face recognition, identity matching, or private-person identification is implemented.
