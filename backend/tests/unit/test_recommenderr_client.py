import httpx
import pytest
from pytest_httpx import HTTPXMock

from backend.clients import recommenderr as rc


@pytest.mark.asyncio
async def test_search_sends_bearer_token(httpx_mock: HTTPXMock, monkeypatch):
    monkeypatch.setattr(rc, "RECOMMENDERR_URL", "http://rec.test")
    monkeypatch.setattr(rc, "RECOMMENDERR_TOKEN", "secret-abc")
    httpx_mock.add_response(json={"videos": []})

    out = await rc.search("foo", page=2)

    assert out == {"videos": []}
    req = httpx_mock.get_requests()[0]
    assert req.headers["authorization"] == "Bearer secret-abc"
    assert req.url.params["q"] == "foo"
    assert req.url.params["page"] == "2"
    assert "/v1/invidious/search" in str(req.url)


@pytest.mark.asyncio
async def test_recognize_calls_correct_path(httpx_mock: HTTPXMock, monkeypatch):
    monkeypatch.setattr(rc, "RECOMMENDERR_URL", "http://rec.test")
    monkeypatch.setattr(rc, "RECOMMENDERR_TOKEN", "t")
    httpx_mock.add_response(json={"is_music": True})

    out = await rc.recognize("abc123")

    assert out == {"is_music": True}
    req = httpx_mock.get_requests()[0]
    assert "/v1/video/abc123/recognize" in str(req.url)
