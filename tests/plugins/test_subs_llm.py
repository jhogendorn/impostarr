from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from impostarr.llm import _JSON_REMINDER, LlmProvider
from impostarr.plugins.base import AssetBundle, ClaimedIdent, SeriesContext
from impostarr_plugin_subs_llm.plugin import SubsLlmConfig, SubsLlmPlugin

BASE_URL = "https://llm.test/v1"


def make_series(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": 1,
        "title": "Show",
        "tvdb_id": 123456,
        "tmdb_id": None,
        "imdb_id": None,
        "title_slug": "show",
    }
    base.update(overrides)
    return base


def make_episode(season: int, episode: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": season * 1000 + episode,
        "season_number": season,
        "episode_number": episode,
        "episode_file_id": 0,
        "has_file": True,
    }
    base.update(overrides)
    return base


def make_claimed(season: int, episodes: list[int]) -> ClaimedIdent:
    return ClaimedIdent(season=season, episodes=episodes, episode_ids=[1])


def make_ctx(series: dict[str, Any], episodes: list[dict[str, Any]]) -> SeriesContext:
    return SeriesContext(series=series, episodes=episodes)


def make_config(**overrides: Any) -> SubsLlmConfig:
    base: dict[str, Any] = {"base_url": BASE_URL, "api_key": "test-key", "model": "test-model"}
    base.update(overrides)
    return SubsLlmConfig(**base)


def write_srt(path: Path, lines: list[str]) -> None:
    blocks = []
    for i, line in enumerate(lines, start=1):
        blocks.append(f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\n{line}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks))


def llm_response(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})


@respx.mock
async def test_match_case_single_merged_claimed_candidate(tmp_path):
    srt = tmp_path / "S01E05.srt"
    write_srt(srt, ["hello there", "general kenobi"])
    episodes = [make_episode(1, n) for n in range(1, 8)]
    ctx = make_ctx(make_series(), episodes)
    claimed = make_claimed(season=1, episodes=[5])
    assets = AssetBundle(sub_paths=[str(srt)])

    route = respx.post(f"{BASE_URL}/chat/completions").mock(
        return_value=llm_response(
            {"season": 1, "episodes": [5], "confidence": 0.9, "reasoning": "matches"}
        )
    )

    plugin = SubsLlmPlugin(make_config())
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    assert route.call_count == 1
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.ident.series == "claimed"
    assert candidate.ident.season == 1
    assert candidate.ident.episodes == [5]
    assert candidate.confidence == 0.9
    assert candidate.numbering == "tvdb"
    assert candidate.evidence["reasoning"] == "matches"
    assert candidate.evidence["model"] == "test-model"
    assert candidate.evidence["provider"] == "default"


@respx.mock
async def test_mislabel_case_two_candidates(tmp_path):
    srt = tmp_path / "S01E05.srt"
    write_srt(srt, ["hello there"])
    episodes = [make_episode(1, n) for n in range(1, 8)]
    ctx = make_ctx(make_series(), episodes)
    claimed = make_claimed(season=1, episodes=[5])
    assets = AssetBundle(sub_paths=[str(srt)])

    respx.post(f"{BASE_URL}/chat/completions").mock(
        return_value=llm_response(
            {"season": 1, "episodes": [7], "confidence": 0.8, "reasoning": "actually ep7"}
        )
    )

    plugin = SubsLlmPlugin(make_config())
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    by_episodes = {tuple(c.ident.episodes): c for c in result.candidates}
    assert set(by_episodes) == {(7,), (5,)}
    assert by_episodes[(7,)].confidence == 0.8
    assert by_episodes[(5,)].confidence == pytest.approx(0.2)
    assert by_episodes[(5,)].ident.series == "claimed"
    assert by_episodes[(5,)].evidence["source"] == "derived"


@respx.mock
async def test_malformed_json_then_retry_succeeds(tmp_path):
    srt = tmp_path / "S01E05.srt"
    write_srt(srt, ["hello there"])
    ctx = make_ctx(make_series(), [make_episode(1, 5)])
    claimed = make_claimed(season=1, episodes=[5])
    assets = AssetBundle(sub_paths=[str(srt)])

    bad_response = httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
    good_response = llm_response(
        {"season": 1, "episodes": [5], "confidence": 0.7, "reasoning": "ok"}
    )
    route = respx.post(f"{BASE_URL}/chat/completions").mock(
        side_effect=[bad_response, good_response]
    )

    plugin = SubsLlmPlugin(make_config())
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    assert route.call_count == 2
    second_body = json.loads(route.calls[1].request.content)
    messages = second_body["messages"]
    reminder_idx = next(i for i, m in enumerate(messages) if _JSON_REMINDER in m["content"])
    assistant_idx = next(i for i, m in enumerate(messages) if m["role"] == "assistant")
    assert messages[assistant_idx]["content"] == "not json"
    assert assistant_idx < reminder_idx


@respx.mock
async def test_malformed_json_twice_returns_error(tmp_path):
    srt = tmp_path / "S01E05.srt"
    write_srt(srt, ["hello there"])
    ctx = make_ctx(make_series(), [make_episode(1, 5)])
    claimed = make_claimed(season=1, episodes=[5])
    assets = AssetBundle(sub_paths=[str(srt)])

    bad_response = httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
    route = respx.post(f"{BASE_URL}/chat/completions").mock(
        side_effect=[bad_response, bad_response]
    )

    plugin = SubsLlmPlugin(make_config())
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "error"
    assert result.reason
    assert route.call_count == 2


async def test_abstain_when_no_sub_paths():
    ctx = make_ctx(make_series(), [make_episode(1, 1)])
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(sub_paths=[])

    plugin = SubsLlmPlugin(make_config())
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "abstain"
    assert result.reason == "no embedded subtitles"


async def test_abstain_when_only_image_subs(tmp_path):
    sup = tmp_path / "S01E01.sup"
    sup.write_bytes(b"binary pgs data")
    ctx = make_ctx(make_series(), [make_episode(1, 1)])
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(sub_paths=[str(sup)])

    plugin = SubsLlmPlugin(make_config())
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "abstain"
    assert result.reason == "PGS/VobSub OCR not supported in PoC"


async def test_abstain_when_no_llm_configured(tmp_path):
    srt = tmp_path / "S01E01.srt"
    write_srt(srt, ["hi"])
    ctx = make_ctx(make_series(), [make_episode(1, 1)])
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(sub_paths=[str(srt)])

    plugin = SubsLlmPlugin(SubsLlmConfig())  # default base_url, no api_key
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "abstain"
    assert result.reason == "no LLM configured"


@respx.mock
async def test_prompt_contains_episode_titles_from_ctx(tmp_path):
    srt = tmp_path / "S01E02.srt"
    write_srt(srt, ["hello there"])
    episodes = [make_episode(1, n, title=f"Title {n}") for n in range(1, 4)]
    ctx = make_ctx(make_series(), episodes)
    claimed = make_claimed(season=1, episodes=[2])
    assets = AssetBundle(sub_paths=[str(srt)])

    route = respx.post(f"{BASE_URL}/chat/completions").mock(
        return_value=llm_response(
            {"season": 1, "episodes": [2], "confidence": 0.5, "reasoning": "r"}
        )
    )

    plugin = SubsLlmPlugin(make_config())
    await plugin.identify(claimed, assets, ctx)

    body = json.loads(route.calls[0].request.content)
    prompt = body["messages"][0]["content"]
    assert "Title 1" in prompt
    assert "Title 2" in prompt
    assert "Title 3" in prompt


@respx.mock
async def test_http_500_returns_error(tmp_path):
    # A persistent 5xx (still 5xx after LlmClient's one built-in retry)
    # exhausts the single configured provider -> surfaces as an error.
    srt = tmp_path / "S01E05.srt"
    write_srt(srt, ["hello there"])
    ctx = make_ctx(make_series(), [make_episode(1, 5)])
    claimed = make_claimed(season=1, episodes=[5])
    assets = AssetBundle(sub_paths=[str(srt)])

    route = respx.post(f"{BASE_URL}/chat/completions").mock(
        return_value=httpx.Response(500, text="boom")
    )

    plugin = SubsLlmPlugin(make_config())
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "error"
    assert result.reason
    assert route.call_count == 2


@respx.mock
async def test_empty_choices_returns_error_not_raise(tmp_path):
    srt = tmp_path / "S01E05.srt"
    write_srt(srt, ["hello there"])
    ctx = make_ctx(make_series(), [make_episode(1, 5)])
    claimed = make_claimed(season=1, episodes=[5])
    assets = AssetBundle(sub_paths=[str(srt)])

    respx.post(f"{BASE_URL}/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    plugin = SubsLlmPlugin(make_config())
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "error"
    assert result.reason


@respx.mock
async def test_episodes_as_string_triggers_retry_path(tmp_path):
    srt = tmp_path / "S01E05.srt"
    write_srt(srt, ["hello there"])
    ctx = make_ctx(make_series(), [make_episode(1, 5)])
    claimed = make_claimed(season=1, episodes=[5])
    assets = AssetBundle(sub_paths=[str(srt)])

    bad_response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"season": 1, "episodes": "5", "confidence": 0.5, "reasoning": "r"}
                        )
                    }
                }
            ]
        },
    )
    good_response = llm_response(
        {"season": 1, "episodes": [5], "confidence": 0.7, "reasoning": "ok"}
    )
    route = respx.post(f"{BASE_URL}/chat/completions").mock(
        side_effect=[bad_response, good_response]
    )

    plugin = SubsLlmPlugin(make_config())
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    assert route.call_count == 2


@respx.mock
async def test_providers_list_takes_precedence_and_falls_through(tmp_path):
    # `providers` set -> base_url/model/api_key are ignored; the first
    # provider is unavailable (429) so the second one serves the request,
    # and its name/model are recorded in evidence.
    srt = tmp_path / "S01E05.srt"
    write_srt(srt, ["hello there"])
    ctx = make_ctx(make_series(), [make_episode(1, 5)])
    claimed = make_claimed(season=1, episodes=[5])
    assets = AssetBundle(sub_paths=[str(srt)])

    respx.post("https://primary.test/v1/chat/completions").mock(return_value=httpx.Response(429))
    respx.post("https://backup.test/v1/chat/completions").mock(
        return_value=llm_response({"season": 1, "episodes": [5], "confidence": 0.6, "reasoning": "r"})
    )

    config = make_config(
        providers=[
            LlmProvider(name="primary", base_url="https://primary.test/v1", model="model-1"),
            LlmProvider(name="backup", base_url="https://backup.test/v1", model="model-2"),
        ]
    )
    plugin = SubsLlmPlugin(config)
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    candidate = result.candidates[0]
    assert candidate.evidence["provider"] == "backup"
    assert candidate.evidence["model"] == "model-2"


@respx.mock
async def test_omits_authorization_header_when_api_key_empty(tmp_path):
    srt = tmp_path / "S01E05.srt"
    write_srt(srt, ["hello there"])
    ctx = make_ctx(make_series(), [make_episode(1, 5)])
    claimed = make_claimed(season=1, episodes=[5])
    assets = AssetBundle(sub_paths=[str(srt)])

    route = respx.post(f"{BASE_URL}/chat/completions").mock(
        return_value=llm_response(
            {"season": 1, "episodes": [5], "confidence": 0.5, "reasoning": "r"}
        )
    )

    plugin = SubsLlmPlugin(make_config(api_key=""))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    assert "authorization" not in {h.lower() for h in route.calls[0].request.headers}
