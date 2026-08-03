import asyncio
import datetime
import json
from pathlib import Path

import httpx
import respx

from impostarr import __version__
from impostarr.config import RefSubsConfig
from impostarr.refsubs import RefSubService

BASE_URL = "https://api.opensubtitles.com/api/v1"
EXPECTED_USER_AGENT = f"Impostarr/{__version__} (github.com/jhogendorn/impostarr)"


def make_cfg(tmp_path: Path, **overrides) -> RefSubsConfig:
    defaults = {
        "api_key": "test-api-key",
        "username": "user",
        "password": "pass",
        "daily_quota": 20,
        "cache_dir": str(tmp_path / "cache"),
        "manual_dir": str(tmp_path / "manual"),
    }
    defaults.update(overrides)
    return RefSubsConfig(**defaults)


async def make_service(cfg: RefSubsConfig) -> RefSubService:
    http = httpx.AsyncClient()
    return RefSubService(cfg, http)


def write_srt(path: Path, text: str = "1\n00:00:00,000 --> 00:00:01,000\nhi\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@respx.mock
async def test_manual_dir_takes_precedence_over_cache(tmp_path):
    cfg = make_cfg(tmp_path)
    manual_path = Path(cfg.manual_dir) / "123456" / "S01E02.srt"
    cache_path = Path(cfg.cache_dir) / "123456" / "S01E02.srt"
    write_srt(manual_path, "manual content")
    write_srt(cache_path, "cache content")

    service = await make_service(cfg)
    result = await service.get({"tvdb": 123456}, season=1, episode=2)

    assert result == manual_path
    assert result.read_text() == "manual content"
    assert len(respx.calls) == 0


@respx.mock
async def test_cache_hit_skips_network(tmp_path):
    cfg = make_cfg(tmp_path)
    cache_path = Path(cfg.cache_dir) / "123456" / "S01E02.srt"
    write_srt(cache_path, "cache content")

    service = await make_service(cfg)
    result = await service.get({"tvdb": 123456}, season=1, episode=2)

    assert result == cache_path
    assert result.read_text() == "cache content"
    assert len(respx.calls) == 0


@respx.mock
async def test_missing_tvdb_id_returns_none_with_no_http_calls(tmp_path):
    cfg = make_cfg(tmp_path)
    service = await make_service(cfg)

    result = await service.get({"imdb": "tt1234567"}, season=1, episode=2)

    assert result is None
    assert len(respx.calls) == 0


@respx.mock
async def test_api_success_path_caches_file_and_increments_quota(tmp_path):
    cfg = make_cfg(tmp_path)
    login_route = respx.post(f"{BASE_URL}/login").mock(
        return_value=httpx.Response(200, json={"token": "jwt-token"})
    )
    search_route = respx.get(f"{BASE_URL}/subtitles").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "download_count": 10,
                            "language": "en",
                            "files": [{"file_id": 111}],
                        }
                    },
                    {
                        "attributes": {
                            "download_count": 500,
                            "language": "en",
                            "files": [{"file_id": 222}],
                        }
                    },
                ]
            },
        )
    )
    download_route = respx.post(f"{BASE_URL}/download").mock(
        return_value=httpx.Response(
            200, json={"link": "https://dl.opensubtitles.com/sub/222.srt", "remaining": 19}
        )
    )
    file_route = respx.get("https://dl.opensubtitles.com/sub/222.srt").mock(
        return_value=httpx.Response(200, text="1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    )

    service = await make_service(cfg)
    result = await service.get({"tvdb": 123456}, season=1, episode=2)

    expected_path = Path(cfg.cache_dir) / "123456" / "S01E02.srt"
    assert result == expected_path
    assert result.read_text() == "1\n00:00:00,000 --> 00:00:01,000\nhello\n"

    assert login_route.called
    assert search_route.called
    assert download_route.called
    assert file_route.called
    # best (highest download_count) file id chosen
    sent_download = json.loads(download_route.calls.last.request.content)
    assert sent_download == {"file_id": 222}
    assert login_route.calls.last.request.headers["Api-Key"] == "test-api-key"
    assert search_route.calls.last.request.headers["Authorization"] == "Bearer jwt-token"
    assert search_route.calls.last.request.url.params["parent_tvdb_id"] == "123456"
    assert search_route.calls.last.request.url.params["season_number"] == "1"
    assert search_route.calls.last.request.url.params["episode_number"] == "2"
    assert search_route.calls.last.request.url.params["languages"] == "en"

    quota_data = json.loads((Path(cfg.cache_dir) / "quota.json").read_text())
    assert quota_data["count"] == 1


@respx.mock
async def test_quota_exhausted_returns_none_with_no_http_calls(tmp_path):
    cfg = make_cfg(tmp_path)
    quota_path = Path(cfg.cache_dir) / "quota.json"
    quota_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    quota_path.write_text(json.dumps({"date": today, "count": cfg.daily_quota}))

    service = await make_service(cfg)
    result = await service.get({"tvdb": 123456}, season=1, episode=2)

    assert result is None
    assert len(respx.calls) == 0


@respx.mock
async def test_quota_resets_on_date_rollover_and_api_proceeds(tmp_path):
    cfg = make_cfg(tmp_path)
    quota_path = Path(cfg.cache_dir) / "quota.json"
    quota_path.parent.mkdir(parents=True, exist_ok=True)
    yesterday = (datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=1)).isoformat()
    quota_path.write_text(json.dumps({"date": yesterday, "count": cfg.daily_quota}))

    respx.post(f"{BASE_URL}/login").mock(return_value=httpx.Response(200, json={"token": "jwt"}))
    respx.get(f"{BASE_URL}/subtitles").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"attributes": {"download_count": 5, "language": "en", "files": [{"file_id": 1}]}}
                ]
            },
        )
    )
    respx.post(f"{BASE_URL}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.opensubtitles.com/sub/1.srt"})
    )
    respx.get("https://dl.opensubtitles.com/sub/1.srt").mock(
        return_value=httpx.Response(200, text="content")
    )

    service = await make_service(cfg)
    result = await service.get({"tvdb": 123456}, season=1, episode=2)

    assert result is not None
    quota_data = json.loads(quota_path.read_text())
    assert quota_data["count"] == 1
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    assert quota_data["date"] == today


@respx.mock
async def test_search_5xx_returns_none_without_crash(tmp_path):
    cfg = make_cfg(tmp_path)
    respx.post(f"{BASE_URL}/login").mock(return_value=httpx.Response(200, json={"token": "jwt"}))
    respx.get(f"{BASE_URL}/subtitles").mock(return_value=httpx.Response(500, text="boom"))

    service = await make_service(cfg)
    result = await service.get({"tvdb": 123456}, season=1, episode=2)

    assert result is None
    assert not (Path(cfg.cache_dir) / "123456" / "S01E02.srt").exists()


@respx.mock
async def test_login_401_then_relogin_once_succeeds(tmp_path):
    cfg = make_cfg(tmp_path)
    login_route = respx.post(f"{BASE_URL}/login").mock(
        side_effect=[
            httpx.Response(200, json={"token": "stale-token"}),
            httpx.Response(200, json={"token": "fresh-token"}),
        ]
    )
    search_route = respx.get(f"{BASE_URL}/subtitles").mock(
        side_effect=[
            httpx.Response(401, json={"message": "expired"}),
            httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "download_count": 5,
                                "language": "en",
                                "files": [{"file_id": 1}],
                            }
                        }
                    ]
                },
            ),
        ]
    )
    respx.post(f"{BASE_URL}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.opensubtitles.com/sub/1.srt"})
    )
    respx.get("https://dl.opensubtitles.com/sub/1.srt").mock(
        return_value=httpx.Response(200, text="content")
    )

    service = await make_service(cfg)
    result = await service.get({"tvdb": 123456}, season=1, episode=2)

    assert result is not None
    assert login_route.call_count == 2
    assert search_route.call_count == 2
    assert search_route.calls[0].request.headers["Authorization"] == "Bearer stale-token"
    assert search_route.calls[1].request.headers["Authorization"] == "Bearer fresh-token"


@respx.mock
async def test_concurrent_get_calls_respect_quota_reservation(tmp_path):
    # One slot left in the quota; two concurrent get() calls (different
    # episodes, same service instance) race for it. The reservation lock
    # must ensure exactly one wins the slot rather than both passing the
    # check before either writes the increment.
    cfg = make_cfg(tmp_path, daily_quota=5)
    quota_path = Path(cfg.cache_dir) / "quota.json"
    quota_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    quota_path.write_text(json.dumps({"date": today, "count": cfg.daily_quota - 1}))

    respx.post(f"{BASE_URL}/login").mock(return_value=httpx.Response(200, json={"token": "jwt"}))
    respx.get(f"{BASE_URL}/subtitles").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "download_count": 5,
                            "language": "en",
                            "files": [{"file_id": 1}],
                        }
                    }
                ]
            },
        )
    )
    respx.post(f"{BASE_URL}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.opensubtitles.com/sub/1.srt"})
    )
    respx.get("https://dl.opensubtitles.com/sub/1.srt").mock(
        return_value=httpx.Response(200, text="content")
    )

    service = await make_service(cfg)
    results = await asyncio.gather(
        service.get({"tvdb": 123456}, season=1, episode=1),
        service.get({"tvdb": 123456}, season=1, episode=2),
    )

    successes = [r for r in results if r is not None]
    failures = [r for r in results if r is None]
    assert len(successes) == 1
    assert len(failures) == 1

    quota_data = json.loads(quota_path.read_text())
    assert quota_data["count"] == cfg.daily_quota


@respx.mock
async def test_user_agent_header_sent_on_every_opensubtitles_request(tmp_path):
    # OpenSubtitles' gateway (kong) 403s requests with no identifying
    # User-Agent (`kong-user-agent-block`) -- every request, including the
    # off-API CDN link fetch, must carry it.
    cfg = make_cfg(tmp_path)
    login_route = respx.post(f"{BASE_URL}/login").mock(
        return_value=httpx.Response(200, json={"token": "jwt"})
    )
    search_route = respx.get(f"{BASE_URL}/subtitles").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"attributes": {"download_count": 5, "language": "en", "files": [{"file_id": 1}]}}
                ]
            },
        )
    )
    download_route = respx.post(f"{BASE_URL}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.opensubtitles.com/sub/1.srt"})
    )
    file_route = respx.get("https://dl.opensubtitles.com/sub/1.srt").mock(
        return_value=httpx.Response(200, text="content")
    )

    service = await make_service(cfg)
    result = await service.get({"tvdb": 123456}, season=1, episode=2)

    assert result is not None
    assert login_route.calls.last.request.headers["User-Agent"] == EXPECTED_USER_AGENT
    assert search_route.calls.last.request.headers["User-Agent"] == EXPECTED_USER_AGENT
    assert download_route.calls.last.request.headers["User-Agent"] == EXPECTED_USER_AGENT
    assert file_route.calls.last.request.headers["User-Agent"] == EXPECTED_USER_AGENT


# -- login single-flight / 429 backoff ----------------------------------


@respx.mock
async def test_concurrent_get_calls_share_a_single_login(tmp_path):
    # whisper-subs gathers up to ~7 refsubs.get() concurrently. Before the
    # fix, each independently saw `_token is None` and POSTed /login,
    # triggering OpenSubtitles' "1 req/sec per IP" login rate limit.
    cfg = make_cfg(tmp_path, daily_quota=20)

    async def _slow_login(request: httpx.Request) -> httpx.Response:
        # A real suspend point, so all 7 concurrent get() calls reach the
        # "_token is None" check before any single login completes --
        # otherwise the mock resolves too fast to ever exercise the race.
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"token": "jwt"})

    login_route = respx.post(f"{BASE_URL}/login").mock(side_effect=_slow_login)
    respx.get(f"{BASE_URL}/subtitles").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"attributes": {"download_count": 5, "language": "en", "files": [{"file_id": 1}]}}
                ]
            },
        )
    )
    respx.post(f"{BASE_URL}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.opensubtitles.com/sub/1.srt"})
    )
    respx.get("https://dl.opensubtitles.com/sub/1.srt").mock(
        return_value=httpx.Response(200, text="content")
    )

    service = await make_service(cfg)
    results = await asyncio.gather(
        *(service.get({"tvdb": 123456}, season=1, episode=ep) for ep in range(1, 8))
    )

    assert all(r is not None for r in results)
    assert login_route.call_count == 1


async def _no_sleep(*args, **kwargs) -> None:
    return None


@respx.mock
async def test_login_429_backs_off_and_retries_once_then_succeeds(tmp_path, monkeypatch):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    cfg = make_cfg(tmp_path)
    login_route = respx.post(f"{BASE_URL}/login").mock(
        side_effect=[
            httpx.Response(429, json={"message": "Login rate limit exceeded: 1 req/sec per IP"}),
            httpx.Response(200, json={"token": "jwt"}),
        ]
    )
    respx.get(f"{BASE_URL}/subtitles").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"attributes": {"download_count": 5, "language": "en", "files": [{"file_id": 1}]}}
                ]
            },
        )
    )
    respx.post(f"{BASE_URL}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.opensubtitles.com/sub/1.srt"})
    )
    respx.get("https://dl.opensubtitles.com/sub/1.srt").mock(
        return_value=httpx.Response(200, text="content")
    )

    service = await make_service(cfg)
    result = await service.get({"tvdb": 123456}, season=1, episode=2)

    assert result is not None
    assert login_route.call_count == 2
    assert len(sleep_calls) == 1
    assert sleep_calls[0] >= 1.1


@respx.mock
async def test_login_429_twice_gives_up_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    cfg = make_cfg(tmp_path)
    login_route = respx.post(f"{BASE_URL}/login").mock(
        return_value=httpx.Response(429, json={"message": "Login rate limit exceeded"})
    )
    search_route = respx.get(f"{BASE_URL}/subtitles").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    service = await make_service(cfg)
    result = await service.get({"tvdb": 123456}, season=1, episode=2)

    assert result is None
    assert login_route.call_count == 2
    assert search_route.call_count == 0
