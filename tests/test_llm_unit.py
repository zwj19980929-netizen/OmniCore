from core.llm import LLMClient


def test_safe_max_tokens_caps_gpt_5_models():
    client = LLMClient(model="openai/GPT-5.4")

    assert client._safe_max_tokens(16000) == 4096


def test_extract_completion_token_limit_from_upstream_error():
    error = Exception(
        "OpenAIException - max_tokens is too large: 16000. "
        "This model supports at most 4096 completion tokens, whereas you provided 16000."
    )

    assert LLMClient._extract_completion_token_limit(error) == 4096


def test_maybe_get_reduced_max_tokens_kwargs_uses_upstream_limit():
    client = LLMClient(model="openai/GPT-5.4")
    kwargs = {"model": "openai/GPT-5.4", "max_tokens": 16000}
    error = Exception(
        "OpenAIException - max_tokens is too large: 16000. "
        "This model supports at most 4096 completion tokens, whereas you provided 16000."
    )

    retry_kwargs = client._maybe_get_reduced_max_tokens_kwargs(kwargs, error)

    assert retry_kwargs is not None
    assert retry_kwargs["max_tokens"] == 4096
