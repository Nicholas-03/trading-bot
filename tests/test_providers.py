import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from llm.providers.base import CompletionResult
from llm.providers.claude import ClaudeProvider
from llm.providers.gemini import GeminiProvider
from llm.providers.deepseek import DeepSeekProvider
from llm.providers.chatgpt import ChatGPTProvider


def test_claude_returns_completion_result():
    mock_msg = MagicMock()
    mock_msg.content[0].text = "claude text"
    mock_msg.usage.input_tokens = 100
    mock_msg.usage.output_tokens = 50

    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider._model = "claude-haiku-4-5"
    provider._client = MagicMock()

    async def run():
        with patch("asyncio.to_thread", new_callable=AsyncMock, return_value=mock_msg):
            return await provider.complete("prompt")

    result = asyncio.run(run())
    assert isinstance(result, CompletionResult)
    assert result.text == "claude text"
    assert result.input_tokens == 100
    assert result.output_tokens == 50


def test_gemini_returns_completion_result():
    mock_resp = MagicMock()
    mock_resp.text = "gemini text"
    mock_resp.usage_metadata.prompt_token_count = 200
    mock_resp.usage_metadata.candidates_token_count = 75

    provider = GeminiProvider.__new__(GeminiProvider)
    provider._model = "gemini-2.5-flash"
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
    provider._client = mock_client

    result = asyncio.run(provider.complete("prompt"))
    assert isinstance(result, CompletionResult)
    assert result.text == "gemini text"
    assert result.input_tokens == 200
    assert result.output_tokens == 75


def test_deepseek_returns_completion_result():
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "deepseek text"
    mock_resp.usage.prompt_tokens = 150
    mock_resp.usage.completion_tokens = 60

    provider = DeepSeekProvider.__new__(DeepSeekProvider)
    provider._model = "deepseek-v4-flash"
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
    provider._client = mock_client

    result = asyncio.run(provider.complete("prompt"))
    assert isinstance(result, CompletionResult)
    assert result.text == "deepseek text"
    assert result.input_tokens == 150
    assert result.output_tokens == 60


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
