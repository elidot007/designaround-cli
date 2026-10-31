"""Idempotent GETs retry gateway errors. POSTs do not. Cloudflare HTML is explained."""

import httpx

from designaround_cli.client import Server
from designaround_cli.output import CliExit


class _Response:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text if text or body is None else ""

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_get_retries_502_then_succeeds(monkeypatch):
    seen = []

    def fake_request(method, url, **kw):
        seen.append(method)
        if len(seen) < 3:
            return _Response(502, text="<!doctype html><html>cloudflare bad gateway</html>")
        return _Response(200, {"ok": True, "data": {"balance": 1}})

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr("designaround_cli.client.time.sleep", lambda _s: None)
    body = Server("http://example.test", api_key="da_test").get("/v1/credits")
    assert body["data"]["balance"] == 1
    assert seen == ["GET", "GET", "GET"]


def test_post_does_not_retry(monkeypatch):
    seen = []

    def fake_request(method, url, **kw):
        seen.append(method)
        return _Response(503, {"detail": {"code": "down", "message": "down"}})

    monkeypatch.setattr(httpx, "request", fake_request)
    try:
        Server("http://example.test", api_key="da_test").post("/v1/screen", {})
    except CliExit as exc:
        assert exc.kind == "down"
    else:
        raise AssertionError("expected CliExit")
    assert seen == ["POST"]


def test_cloudflare_html_is_bilingual(monkeypatch):
    def fake_request(method, url, **kw):
        return _Response(502, text="<!DOCTYPE html><html><title>cloudflare</title>error</html>")

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr("designaround_cli.client._RETRY_DELAYS", ())
    try:
        Server("http://example.test", api_key="da_test").get("/v1/catalog")
    except CliExit as exc:
        assert exc.kind == "cloudflare_error"
        assert "Cloudflare" in exc.message and "暂时不可用" in exc.message
        assert "<html" not in exc.message
    else:
        raise AssertionError("expected CliExit")
