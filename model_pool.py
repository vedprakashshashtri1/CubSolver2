import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import List

from google import genai
from google.genai import types

from prompts import HTML_SOLUTION_PROMPT

logger = logging.getLogger("Cub.ModelPool")


def _is_retryable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    retry_markers = (
        "429",
        "resource_exhausted",
        "rate limit",
        "quota",
        "503",
        "service unavailable",
        "unavailable",
        "timeout",
        "temporarily",
        "overloaded",
        "500",
        "502",
        "504",
    )
    return any(marker in text for marker in retry_markers)


@dataclass
class ModelState:
    name: str
    concurrency: int
    semaphore: asyncio.Semaphore = field(init=False)
    successes: int = 0
    failures: int = 0
    last_error: str = ""
    last_used_at: float = 0.0

    def __post_init__(self):
        self.semaphore = asyncio.Semaphore(self.concurrency)


class GeminiModelPool:
    """
    Distributes DIFFERENT users across multiple Gemini models.

    It does NOT fan out one question to every model because that wastes quota.
    Each request starts on one model; if that model is rate-limited or
    temporarily unavailable, the request automatically falls back to another.
    """

    def __init__(
        self,
        api_key: str,
        model_names: List[str],
        per_model_concurrency: int = 2,
        timeout_seconds: int = 120,
    ):
        if not model_names:
            raise ValueError("At least one Gemini model must be configured.")

        self.client = genai.Client(api_key=api_key)
        self.models = [
            ModelState(name=m, concurrency=per_model_concurrency)
            for m in model_names
        ]
        self.timeout_seconds = timeout_seconds
        self._rr_lock = asyncio.Lock()
        self._next_index = 0

    async def _starting_index(self) -> int:
        async with self._rr_lock:
            idx = self._next_index
            self._next_index = (self._next_index + 1) % len(self.models)
            return idx

    async def _generate_with_model(
        self,
        state: ModelState,
        image_bytes: bytes,
        mime_type: str,
        user_instruction: str = "",
    ) -> str:
        prompt = HTML_SOLUTION_PROMPT

        if user_instruction.strip():
            prompt += (
                "\n\nUSER'S ADDITIONAL INSTRUCTION:\n"
                + user_instruction.strip()
                + "\nFollow it while still solving step by step."
            )

        content = types.Content(
            role="user",
            parts=[
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type=mime_type,
                ),
                types.Part(text=prompt),
            ],
        )

        async with state.semaphore:
            state.last_used_at = time.time()

            response = await asyncio.wait_for(
                self.client.aio.models.generate_content(
                    model=state.name,
                    contents=content,
                    config=types.GenerateContentConfig(
                        temperature=0.15,
                    ),
                ),
                timeout=self.timeout_seconds,
            )

        html_text = getattr(response, "text", None)

        if not html_text or not html_text.strip():
            raise RuntimeError(f"{state.name} returned an empty response.")

        return html_text.strip()

    async def generate_html(
        self,
        image_bytes: bytes,
        mime_type: str,
        user_instruction: str = "",
    ):
        start = await self._starting_index()
        errors = []

        # Try each configured model once, beginning from a rotating index.
        for offset in range(len(self.models)):
            state = self.models[(start + offset) % len(self.models)]

            try:
                logger.info("Trying model: %s", state.name)

                result = await self._generate_with_model(
                    state=state,
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                    user_instruction=user_instruction,
                )

                state.successes += 1
                state.last_error = ""

                return result, state.name

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                state.failures += 1
                state.last_error = str(exc)[:300]
                errors.append(f"{state.name}: {exc}")

                logger.warning(
                    "Model %s failed: %s",
                    state.name,
                    exc,
                )

                # For a permanent/non-retryable error, still try other models:
                # one configured model may not support the input while another does.
                if _is_retryable_error(exc):
                    await asyncio.sleep(0.35)

        raise RuntimeError(
            "All configured Gemini models failed.\n"
            + "\n".join(errors[-len(self.models):])
        )

    def model_names(self):
        return [m.name for m in self.models]

    def stats(self):
        return [
            {
                "name": m.name,
                "successes": m.successes,
                "failures": m.failures,
                "last_error": m.last_error,
            }
            for m in self.models
        ]

    async def close(self):
        try:
            await self.client.aio.aclose()
        except Exception:
            pass


def parse_model_list(raw: str):
    return [
        item.strip()
        for item in raw.split(",")
        if item.strip()
    ]
