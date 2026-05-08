import asyncio
from unittest.mock import AsyncMock, MagicMock
from llm.providers.base import CompletionResult
from llm.providers.chatgpt import ChatGPTProvider


def test_chatgpt_returns_completion_result():
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "chatgpt text"
    mock_resp.usage.prompt_tokens = 120
    mock_resp.usage.completion_tokens = 45

    provider = ChatGPTProvider.__new__(ChatGPTProvider)
    provider._model = "gpt-5.4-mini"
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    provider._client = mock_client

    result = asyncio.run(provider.complete("prompt"))
    assert isinstance(result, CompletionResult)
    assert result.text == "chatgpt text"
    assert result.input_tokens == 120
    assert result.output_tokens == 45
