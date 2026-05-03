import asyncio
from anthropic import Anthropic
from llm.providers.base import CompletionResult


class ClaudeProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = Anthropic(api_key=api_key)
        self._model = model

    async def complete(self, prompt: str) -> CompletionResult:
        message = await asyncio.to_thread(
            self._client.messages.create,
            model=self._model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return CompletionResult(
            text=message.content[0].text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )
