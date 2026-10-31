"""HTTP client for the thin server."""

from __future__ import annotations

import time

import httpx

from . import __version__, config
from .output import EXIT_AUTH, EXIT_BUDGET, EXIT_ERROR, EXIT_INVALID, CliExit

# Idempotent GETs only. A POST that got a 502 may have been applied.
_RETRY_STATUSES = {502, 503, 504}
_RETRY_DELAYS = (0.4, 1.2)


class Server:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or config.server_url()).rstrip("/")
        self.api_key = api_key if api_key is not None else config.api_key()

    def _headers(self, auth: bool) -> dict:
        headers = {"User-Agent": f"designaround-cli/{__version__}"}
        if auth:
            if not self.api_key:
                raise CliExit(EXIT_AUTH, "auth_required", "No API key configured.", next_actions=["designaround onboard", "designaround auth login --key da_..."])
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def request(self, method: str, path: str, *, auth: bool = True, json: dict | None = None, params: dict | None = None, timeout: float = 120.0, content=None) -> dict:
        headers = self._headers(auth)
        if content is not None:
            headers["Content-Type"] = "application/octet-stream"
        attempts = 1 + len(_RETRY_DELAYS) if method.upper() == "GET" else 1
        response = None
        for attempt in range(attempts):
            try:
                response = httpx.request(method, f"{self.base_url}{path}", headers=headers, json=json, params=params, timeout=timeout, content=content)
            except httpx.HTTPError as exc:
                raise CliExit(EXIT_ERROR, "network_error", f"Cannot reach {self.base_url}: {exc}", next_actions=["Check your connection or DESIGNAROUND_BASE_URL; retry."]) from exc
            if response.status_code in _RETRY_STATUSES and attempt + 1 < attempts:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            break
        assert response is not None
        cloudflare = _cloudflare_message(response.status_code, response.text)
        try:
            body = response.json()
        except ValueError:
            if cloudflare:
                raise CliExit(EXIT_ERROR, "cloudflare_error", cloudflare, data={"status": response.status_code}, next_actions=["Wait a moment and retry the same command.", "稍后再执行同一条命令。"]) from None
            body = {"detail": response.text[:500]}
        if response.status_code in (401, 403):
            detail = body.get("detail") if isinstance(body, dict) else body
            raise CliExit(EXIT_AUTH, "auth_failed", (detail or {}).get("message", "Authentication failed") if isinstance(detail, dict) else str(detail), data=detail, next_actions=["designaround auth status", "designaround onboard"])
        if response.status_code == 402:
            detail = body.get("detail", {})
            raise CliExit(EXIT_BUDGET, detail.get("code", "insufficient_credits"), detail.get("message", "Not enough credits"), data=detail, next_actions=["designaround credits", "designaround billing checkout --credits 5000  (a person completes payment in the browser)"])
        if response.status_code in (400, 404, 409, 413, 422):
            detail = body.get("detail", body)
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            raise CliExit(EXIT_INVALID, (detail.get("code") if isinstance(detail, dict) else None) or "invalid_request", message or "Request rejected", data=detail)
        if response.status_code == 429:
            detail = body.get("detail", body) if isinstance(body, dict) else body
            raise CliExit(EXIT_ERROR, (detail.get("code") if isinstance(detail, dict) else None) or "rate_limited", (detail.get("message") if isinstance(detail, dict) else None) or "Too many requests; retry later.", data=detail)
        if response.status_code >= 500:
            if cloudflare:
                raise CliExit(EXIT_ERROR, "cloudflare_error", cloudflare, data={"status": response.status_code}, next_actions=["Wait a moment and retry the same command.", "稍后再执行同一条命令。"]) from None
            detail = body.get("detail", body) if isinstance(body, dict) else body
            code = (detail.get("code") if isinstance(detail, dict) else None) or "server_error"
            message = (detail.get("message") if isinstance(detail, dict) else None) or f"Server error {response.status_code}"
            raise CliExit(EXIT_ERROR, code, message, data=detail)
        if response.status_code >= 400:  # 405, 410, 415, ...: never treat as success
            raise CliExit(EXIT_INVALID, f"http_{response.status_code}", f"Server answered {response.status_code} for {method} {path}", data=body)
        return body

    def get(self, path: str, **kw) -> dict:
        return self.request("GET", path, **kw)

    def post(self, path: str, json: dict | None = None, **kw) -> dict:
        return self.request("POST", path, json=json, **kw)

    def put_file(self, path: str, file: str, timeout: float = 600.0) -> dict:
        """Stream a local file as the raw request body."""

        with open(file, "rb") as handle:
            return self.request("PUT", path, content=handle, timeout=timeout)

    def delete(self, path: str, **kw) -> dict:
        return self.request("DELETE", path, **kw)

    def download(self, url: str, timeout: float = 60.0) -> bytes:
        response = httpx.get(url, headers={"User-Agent": f"designaround-cli/{__version__}"}, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        return response.content


def _cloudflare_message(status: int, text: str) -> str | None:
    """A Cloudflare HTML error page is not a JSON error from our server."""

    sample = (text or "")[:4000].lower()
    if "<html" not in sample and "<!doctype html" not in sample:
        return None
    if "cloudflare" not in sample and "cf-ray" not in sample and "cf-error" not in sample:
        return None
    return (
        f"服务暂时不可用（Cloudflare HTTP {status}）。请稍后再试。 / "
        f"The service is temporarily unavailable (Cloudflare HTTP {status}). Try again in a moment."
    )
