"""Single-frame Gemini 3.7 Flash vision escalation with validated structured output."""

from __future__ import annotations

import asyncio
import io
from typing import Any, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from ..config import GeminiConfig, VisionConfig


class GeminiVisionResult(BaseModel):
    """Validated interpretation of one selected camera frame."""

    model_config = ConfigDict(extra="forbid")
    event_type: Literal["wave", "person_arrived", "person_left", "object_shown", "text_visible", "unknown"]
    description: str = Field(min_length=1, max_length=800)
    confidence: float = Field(ge=0.0, le=1.0)
    should_assistant_react: bool
    suggested_context: str = Field(default="", max_length=800)


class GeminiVisionClient:
    """Upload exactly one compressed representative frame per explicit analysis call."""

    def __init__(self, api_key: str, gemini: GeminiConfig, vision: VisionConfig) -> None:
        self.api_key = api_key
        self.gemini = gemini
        self.vision = vision
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise RuntimeError("google-genai is not installed") from exc
            from google.genai import types

            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    timeout=int(self.gemini.retry.read_timeout_sec * 1000),
                    retry_options=types.HttpRetryOptions(
                        attempts=4,
                        initial_delay=self.gemini.retry.initial_backoff_sec,
                        max_delay=self.gemini.retry.max_backoff_sec,
                        exp_base=2.0,
                        jitter=self.gemini.retry.jitter_ratio,
                    ),
                ),
            )
        return self._client

    async def analyze_frame(self, frame: Any, *, question: str) -> GeminiVisionResult:
        """Analyze one image; this method never loops or uploads adjacent video frames."""
        from google.genai import types

        jpeg = await asyncio.to_thread(self._to_jpeg, frame)
        prompt = (
            "Analyze only this single camera frame. Do not identify private people. "
            "If uncertain, use event_type='unknown' and a correspondingly low confidence. "
            "Return the requested schema. Question/context: " + question
        )
        response = await self._get_client().aio.models.generate_content(
            model=self.gemini.brain_model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeminiVisionResult,
            ),
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, GeminiVisionResult):
            return parsed
        text = getattr(response, "text", "") or ""
        return GeminiVisionResult.model_validate_json(text)

    def _to_jpeg(self, frame: Any) -> bytes:
        import numpy as np

        array = np.asarray(frame)
        image = Image.fromarray(array.astype("uint8"), mode="RGB")
        if image.width > self.vision.cloud_max_width:
            ratio = self.vision.cloud_max_width / image.width
            image = image.resize((self.vision.cloud_max_width, max(1, int(image.height * ratio))))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=self.vision.jpeg_quality, optimize=True)
        return buffer.getvalue()


class MockVisionClient:
    """Deterministic development/test vision backend; never used unless mock mode is explicit."""

    def __init__(self, result: GeminiVisionResult | None = None) -> None:
        self.result = result or GeminiVisionResult(
            event_type="unknown",
            description="Mock vision saw an intentionally unspecified fixture event.",
            confidence=0.45,
            should_assistant_react=False,
            suggested_context="development fixture",
        )
        self.calls = 0

    async def analyze_frame(self, frame: Any, *, question: str) -> GeminiVisionResult:
        del frame, question
        self.calls += 1
        await asyncio.sleep(0)
        return self.result
