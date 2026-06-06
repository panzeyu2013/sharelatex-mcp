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

    def get(self, path: str, **kwargs: Any) -> HttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("GET %s", url)
        try:
            response = self.session.get(
                url, timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            raise _wrap_network_error(self.base_url, self.timeout_seconds, exc) from exc
        return HttpResult(
            status_code=response.status_code,
            headers=response.headers,
            text=response.text,
            url=response.url,
        )

    def get_absolute(self, absolute_url: str, **kwargs: Any) -> HttpResult:
        logger.debug("GET(absolute) %s", absolute_url)
        try:
            response = self.session.get(absolute_url, timeout=self.timeout_seconds, **kwargs)
        except requests.RequestException as exc:
            raise _wrap_network_error(self.base_url, self.timeout_seconds, exc) from exc
        return HttpResult(
            status_code=response.status_code,
            headers=response.headers,
            text=response.text,
            url=response.url,
        )

    def post_form(self, path: str, data: dict[str, str], **kwargs: Any) -> HttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("POST(form) %s", url)
        try:
            response = self.session.post(
                url, data=data, timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            raise _wrap_network_error(self.base_url, self.timeout_seconds, exc) from exc
        return HttpResult(
            status_code=response.status_code,
            headers=response.headers,
            text=response.text,
            url=response.url,
        )

    def post_json(self, path: str, payload: dict[str, Any], **kwargs: Any) -> HttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("POST(json) %s", url)
        try:
            response = self.session.post(
                url, json=payload, timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            raise _wrap_network_error(self.base_url, self.timeout_seconds, exc) from exc
        return HttpResult(
            status_code=response.status_code,
            headers=response.headers,
            text=response.text,
            url=response.url,
        )

    def post_multipart(
        self,
        path: str,
        files: dict[str, tuple[str, bytes, str]],
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> HttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("POST(multipart) %s", url)
        try:
            response = self.session.post(
                url, files=files, data=data, timeout=self.timeout_seconds,
                allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            raise _wrap_network_error(self.base_url, self.timeout_seconds, exc) from exc
        return HttpResult(
            status_code=response.status_code,
            headers=response.headers,
            text=response.text,
            url=response.url,
        )

    def delete(self, path: str, **kwargs: Any) -> HttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("DELETE %s", url)
        try:
            response = self.session.delete(
                url, timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            raise _wrap_network_error(self.base_url, self.timeout_seconds, exc) from exc
        return HttpResult(
            status_code=response.status_code,
            headers=response.headers,
            text=response.text,
            url=response.url,
        )

    def get_bytes(self, path: str, **kwargs: Any) -> BinaryHttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("GET(bytes) %s", url)
        try:
            response = self.session.get(
                url, timeout=self.timeout_seconds, allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            raise _wrap_network_error(self.base_url, self.timeout_seconds, exc) from exc
        return BinaryHttpResult(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            url=response.url,
        )

    def get_bytes_absolute(self, absolute_url: str, **kwargs: Any) -> BinaryHttpResult:
        logger.debug("GET(bytes, absolute) %s", absolute_url)
        try:
            response = self.session.get(absolute_url, timeout=self.timeout_seconds, **kwargs)
        except requests.RequestException as exc:
            raise _wrap_network_error(self.base_url, self.timeout_seconds, exc) from exc
        return BinaryHttpResult(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            url=response.url,
        )
