import asyncio
import logging

import openai

logger = logging.getLogger(__name__)


class DeepSeekProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )
        self._model = model

    async def complete(self, prompt: str) -> str:
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.choices[0].message.content
            except openai.APIStatusError as e:
                if e.status_code >= 500 and attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "DeepSeek 5xx (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, max_retries, wait, e,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
