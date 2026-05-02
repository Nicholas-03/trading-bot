import asyncio
import logging

import openai

logger = logging.getLogger(__name__)


class ChatGPTProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key)
        self._model = model

    async def complete(self, prompt: str) -> str:
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    max_completion_tokens=512,
                    messages=[{"role": "user", "content": prompt}],
                )
                content = response.choices[0].message.content
                if content is None:
                    raise ValueError("OpenAI returned no text content (finish_reason may be non-stop)")
                return content
            except openai.APIStatusError as e:
                if e.status_code >= 500 and attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "ChatGPT 5xx (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, max_retries, wait, e,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
