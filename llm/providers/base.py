from typing import Protocol


class LLMProvider(Protocol):
    async def complete(self, prompt: str) -> str: ...
