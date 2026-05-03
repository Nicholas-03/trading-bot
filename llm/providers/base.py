from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int


class LLMProvider(Protocol):
    async def complete(self, prompt: str) -> CompletionResult: ...
