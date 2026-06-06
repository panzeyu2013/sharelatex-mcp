from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests
from requests.structures import CaseInsensitiveDict

logger = logging.getLogger(__name__)


@dataclass
class HttpResult:
    status_code: int
    headers: CaseInsensitiveDict
    text: str
    url: str


@dataclass
class BinaryHttpResult:
    status_code: int
    headers: CaseInsensitiveDict
    content: bytes
    url: str


def _wrap_network_error(base_url: str, timeout: int, exc: Exception) -> RuntimeError:
    if isinstance(exc, requests.Timeout):
        return RuntimeError(f"Request timed out after {timeout}s")
    if isinstance(exc, requests.ConnectionError):
        return RuntimeError(f"Failed to connect to Overleaf at {base_url}")
    return RuntimeError(f"HTTP request failed: {exc}")


class HttpClient:
    def __init__(self, base_url: str, timeout_seconds: int) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "sharelatex-mcp/0.1.0"})
        logger.debug("HttpClient initialized: base_url=%s timeout=%ds", base_url, timeout_seconds)

    def close(self) -> None:
        self.session.close()
        logger.debug("HttpClient session closed")

    def _build_url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _request_text(self, method: str, url: str, **kwargs: Any) -> HttpResult:
        logger.debug("%s %s", method.upper(), url)
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise _wrap_network_error(self.base_url, self.timeout_seconds, exc) from exc
        return HttpResult(
            status_code=response.status_code,
            headers=response.headers,
            text=response.text,
            url=response.url,
        )

    def _request_bytes(self, method: str, url: str, **kwargs: Any) -> BinaryHttpResult:
        logger.debug("%s(bytes) %s", method.upper(), url)
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise _wrap_network_error(self.base_url, self.timeout_seconds, exc) from exc
        return BinaryHttpResult(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            url=response.url,
        )

    def get(self, path: str, **kwargs: Any) -> HttpResult:
        return self._request_text(
            "GET", self._build_url(path),
            timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
        )

    def get_absolute(self, absolute_url: str, **kwargs: Any) -> HttpResult:
        return self._request_text(
            "GET", absolute_url,
            timeout=self.timeout_seconds, **kwargs,
        )

    def post_form(self, path: str, data: dict[str, str], **kwargs: Any) -> HttpResult:
        return self._request_text(
            "POST", self._build_url(path), data=data,
            timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
        )

    def post_json(self, path: str, payload: dict[str, Any], **kwargs: Any) -> HttpResult:
        return self._request_text(
            "POST", self._build_url(path), json=payload,
            timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
        )

    def post_multipart(
        self,
        path: str,
        files: dict[str, tuple[str, bytes, str]],
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> HttpResult:
        return self._request_text(
            "POST", self._build_url(path), files=files, data=data,
            timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
        )

    def delete(self, path: str, **kwargs: Any) -> HttpResult:
        return self._request_text(
            "DELETE", self._build_url(path),
            timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
        )

    def get_bytes(self, path: str, **kwargs: Any) -> BinaryHttpResult:
        return self._request_bytes(
            "GET", self._build_url(path),
            timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
        )

    def get_bytes_absolute(self, absolute_url: str, **kwargs: Any) -> BinaryHttpResult:
        return self._request_bytes(
            "GET", absolute_url,
            timeout=self.timeout_seconds, **kwargs,
        )
