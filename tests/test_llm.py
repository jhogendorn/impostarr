from __future__ import annotations

import json

import httpx
import respx

from impostarr.llm import LlmClient, LlmContentError, LlmProvider, LlmUnavailable

PROVIDER_A = LlmProvider(name="a", base_url="https://a.test/v1", model="model-a", api_key="key-a")
PROVIDER_B = LlmProvider(name="b", base_url="https://b.test/v1", model="model-b", api_key="key-b")


def _ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})


async def make_client(providers: list[LlmProvider]) -> LlmClient:
    return LlmClient(providers, http=httpx.AsyncClient())


@respx.mock
async def test_single_provider_success_returns_provider_name():
    respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(return_value=_ok({"x": 1}))
    client = await make_client([PROVIDER_A])

    data, provider_name = await client.chat_json([{"role": "user", "content": "hi"}])

    assert data == {"x": 1}
    assert provider_name == "a"


@respx.mock
async def test_fallthrough_on_401():
    route_a = respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    respx.post(f"{PROVIDER_B.base_url}/chat/completions").mock(return_value=_ok({"x": 2}))
    client = await make_client([PROVIDER_A, PROVIDER_B])

    data, provider_name = await client.chat_json([{"role": "user", "content": "hi"}])

    assert data == {"x": 2}
    assert provider_name == "b"
    assert route_a.call_count == 1


@respx.mock
async def test_fallthrough_on_402():
    respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(return_value=httpx.Response(402))
    respx.post(f"{PROVIDER_B.base_url}/chat/completions").mock(return_value=_ok({"x": 2}))
    client = await make_client([PROVIDER_A, PROVIDER_B])

    _data, provider_name = await client.chat_json([{"role": "user", "content": "hi"}])

    assert provider_name == "b"


@respx.mock
async def test_fallthrough_on_403():
    respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(return_value=httpx.Response(403))
    respx.post(f"{PROVIDER_B.base_url}/chat/completions").mock(return_value=_ok({"x": 2}))
    client = await make_client([PROVIDER_A, PROVIDER_B])

    _data, provider_name = await client.chat_json([{"role": "user", "content": "hi"}])

    assert provider_name == "b"


@respx.mock
async def test_fallthrough_on_429():
    respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(return_value=httpx.Response(429))
    respx.post(f"{PROVIDER_B.base_url}/chat/completions").mock(return_value=_ok({"x": 2}))
    client = await make_client([PROVIDER_A, PROVIDER_B])

    _data, provider_name = await client.chat_json([{"role": "user", "content": "hi"}])

    assert provider_name == "b"


@respx.mock
async def test_fallthrough_on_insufficient_quota_body():
    respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(
        return_value=httpx.Response(
            400, json={"error": {"code": "insufficient_quota", "message": "you broke"}}
        )
    )
    respx.post(f"{PROVIDER_B.base_url}/chat/completions").mock(return_value=_ok({"x": 2}))
    client = await make_client([PROVIDER_A, PROVIDER_B])

    _data, provider_name = await client.chat_json([{"role": "user", "content": "hi"}])

    assert provider_name == "b"


@respx.mock
async def test_fallthrough_on_persistent_5xx_after_one_retry():
    route_a = respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(
        return_value=httpx.Response(500, text="boom")
    )
    respx.post(f"{PROVIDER_B.base_url}/chat/completions").mock(return_value=_ok({"x": 2}))
    client = await make_client([PROVIDER_A, PROVIDER_B])

    _data, provider_name = await client.chat_json([{"role": "user", "content": "hi"}])

    assert provider_name == "b"
    assert route_a.call_count == 2  # one retry before giving up on provider A


@respx.mock
async def test_transient_5xx_then_success_does_not_fall_through():
    route_a = respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(
        side_effect=[httpx.Response(500, text="boom"), _ok({"x": 1})]
    )
    client = await make_client([PROVIDER_A, PROVIDER_B])

    data, provider_name = await client.chat_json([{"role": "user", "content": "hi"}])

    assert provider_name == "a"
    assert data == {"x": 1}
    assert route_a.call_count == 2


@respx.mock
async def test_fallthrough_on_transport_error():
    respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(side_effect=httpx.ConnectError("down"))
    respx.post(f"{PROVIDER_B.base_url}/chat/completions").mock(return_value=_ok({"x": 2}))
    client = await make_client([PROVIDER_A, PROVIDER_B])

    _data, provider_name = await client.chat_json([{"role": "user", "content": "hi"}])

    assert provider_name == "b"


@respx.mock
async def test_no_fallthrough_on_malformed_json_after_retry():
    route_a = respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
    )
    route_b = respx.post(f"{PROVIDER_B.base_url}/chat/completions").mock(return_value=_ok({"x": 2}))
    client = await make_client([PROVIDER_A, PROVIDER_B])

    try:
        await client.chat_json([{"role": "user", "content": "hi"}])
        raised = False
    except LlmContentError:
        raised = True

    assert raised
    assert route_a.call_count == 2  # original + one reminder retry
    assert route_b.call_count == 0  # never falls through to B


@respx.mock
async def test_malformed_json_then_reminder_retry_succeeds():
    route_a = respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}),
            _ok({"x": 1}),
        ]
    )
    client = await make_client([PROVIDER_A])

    data, _provider_name = await client.chat_json([{"role": "user", "content": "hi"}])

    assert data == {"x": 1}
    assert route_a.call_count == 2


@respx.mock
async def test_validate_callback_triggers_reminder_retry():
    route_a = respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(
        side_effect=[_ok({"x": "bad shape"}), _ok({"x": 1})]
    )
    client = await make_client([PROVIDER_A])

    data, _provider_name = await client.chat_json(
        [{"role": "user", "content": "hi"}], validate=lambda d: isinstance(d.get("x"), int)
    )

    assert data == {"x": 1}
    assert route_a.call_count == 2


@respx.mock
async def test_all_providers_unavailable_raises():
    respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(return_value=httpx.Response(429))
    respx.post(f"{PROVIDER_B.base_url}/chat/completions").mock(return_value=httpx.Response(401))
    client = await make_client([PROVIDER_A, PROVIDER_B])

    try:
        await client.chat_json([{"role": "user", "content": "hi"}])
        raised = False
    except LlmUnavailable:
        raised = True

    assert raised


@respx.mock
async def test_other_4xx_is_content_error_no_fallthrough():
    respx.post(f"{PROVIDER_A.base_url}/chat/completions").mock(return_value=httpx.Response(400))
    route_b = respx.post(f"{PROVIDER_B.base_url}/chat/completions").mock(return_value=_ok({"x": 2}))
    client = await make_client([PROVIDER_A, PROVIDER_B])

    try:
        await client.chat_json([{"role": "user", "content": "hi"}])
        raised = False
    except LlmContentError:
        raised = True

    assert raised
    assert route_b.call_count == 0
