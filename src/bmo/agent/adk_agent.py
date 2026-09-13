"""Current Google ADK runner configured for Gemini 3.7 Flash SSE text streaming."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from ..config import GeminiConfig
from .system_prompt import SYSTEM_PROMPT
from .tools import ToolRegistry


class ADKAgentRuntime:
    """Own an ADK App/Runner/session and yield only incremental model text."""

    APP_NAME = "bmo_pi"
    USER_ID = "local_user"
    SESSION_ID = "bmo_device_session"

    def __init__(self, config: GeminiConfig, api_key: str, tools: ToolRegistry) -> None:
        self.config = config
        self.api_key = api_key
        self.tools = tools
        self._runner: Any = None
        self._types: Any = None
        self._run_config_cls: Any = None
        self._streaming_mode: Any = None

    async def initialize(self) -> None:
        # ADK's Gemini API authentication docs use GOOGLE_API_KEY. Keep the user's
        # GEMINI_API_KEY external contract and bridge it internally without logging it.
        os.environ.setdefault("GOOGLE_API_KEY", self.api_key)
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
        try:
            from google.adk.agents import Agent
            from google.adk.agents.run_config import RunConfig, StreamingMode
            from google.adk.apps import App
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
            from google.adk.models import Gemini
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("google-adk/google-genai not installed; run scripts/install.sh") from exc

        model = Gemini(
            model=self.config.brain_model,
            retry_options=types.HttpRetryOptions(
                attempts=4,
                initial_delay=self.config.retry.initial_backoff_sec,
                max_delay=self.config.retry.max_backoff_sec,
                exp_base=2.0,
                jitter=self.config.retry.jitter_ratio,
            ),
        )
        agent = Agent(
            name="beemo_companion",
            model=model,
            description="Original small embodied companion with constrained local hardware tools.",
            instruction=SYSTEM_PROMPT,
            tools=self.tools.adk_functions(),
            # Keep spoken replies short to reduce first-audio latency and cost.
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=220,
                thinking_config=types.ThinkingConfig(thinking_level="low"),
            ),
        )
        app = App(name=self.APP_NAME, root_agent=agent)
        sessions = InMemorySessionService()
        await sessions.create_session(app_name=self.APP_NAME, user_id=self.USER_ID, session_id=self.SESSION_ID)
        self._runner = Runner(app=app, session_service=sessions)
        self._types = types
        self._run_config_cls = RunConfig
        self._streaming_mode = StreamingMode

    async def stream(self, prompt: str, *, max_llm_calls: int | None = None) -> AsyncIterator[str]:
        if self._runner is None:
            await self.initialize()
        content = self._types.Content(role="user", parts=[self._types.Part(text=prompt)])
        run_config = self._run_config_cls(
            streaming_mode=self._streaming_mode.SSE,
            max_llm_calls=max_llm_calls or self.config.max_llm_calls,
        )
        saw_partial = False
        async for event in self._runner.run_async(
            user_id=self.USER_ID,
            session_id=self.SESSION_ID,
            new_message=content,
            run_config=run_config,
        ):
            if not event.content or not event.content.parts:
                continue
            text = "".join(part.text or "" for part in event.content.parts if getattr(part, "text", None))
            if not text:
                continue
            if event.partial:
                saw_partial = True
                yield text
            elif event.is_final_response() and not saw_partial:
                # Non-streaming fallback if a model/backend returns only a final event.
                yield text
