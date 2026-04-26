import asyncio
import logging

from google import genai
from google.genai import errors as genai_errors

logger = logging.getLogger(__name__)


class GeminiProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def complete(self, prompt: str) -> str:
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=prompt,
                )
                return response.text
            except genai_errors.ServerError as e:
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "Gemini 503 (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, max_retries, wait, e,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
