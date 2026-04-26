import asyncio

from anthropic import Anthropic


class ClaudeProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = Anthropic(api_key=api_key)
        self._model = model

    async def complete(self, prompt: str) -> str:
        message = await asyncio.to_thread(
            self._client.messages.create,
            model=self._model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
