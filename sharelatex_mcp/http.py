from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)


@dataclass
class HttpResult:
    status_code: int
    headers: dict[str, str]
    text: str
    url: str
    _response: Any = None


@dataclass
class BinaryHttpResult:
    status_code: int
    headers: dict[str, str]
    content: bytes
    url: str


class HttpClient:
    def __init__(self, base_url: str, timeout_seconds: int) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "sharelatex-mcp/0.1.0",
            }
        )
        logger.debug("HttpClient initialized: base_url=%s timeout=%ds", base_url, timeout_seconds)

    def get(self, path: str, **kwargs: Any) -> HttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("GET %s", url)
        response = self.session.get(
            url,
            timeout=self.timeout_seconds,
            allow_redirects=False,
            **kwargs,
        )
        return HttpResult(
            status_code=response.status_code,
            headers=dict(response.headers),
            text=response.text,
            url=response.url,
            _response=response,
        )

    def post_form(self, path: str, data: dict[str, str], **kwargs: Any) -> HttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("POST(form) %s", url)
        response = self.session.post(
            url,
            data=data,
            timeout=self.timeout_seconds,
            allow_redirects=False,
            **kwargs,
        )
        return HttpResult(
            status_code=response.status_code,
            headers=dict(response.headers),
            text=response.text,
            url=response.url,
            _response=response,
        )

    def post_json(self, path: str, payload: dict[str, Any], **kwargs: Any) -> HttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("POST(json) %s", url)
        response = self.session.post(
            url,
            json=payload,
            timeout=self.timeout_seconds,
            allow_redirects=False,
            **kwargs,
        )
        return HttpResult(
            status_code=response.status_code,
            headers=dict(response.headers),
            text=response.text,
            url=response.url,
            _response=response,
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
        response = self.session.post(
            url,
            files=files,
            data=data,
            timeout=self.timeout_seconds,
            allow_redirects=False,
            **kwargs,
        )
        return HttpResult(
            status_code=response.status_code,
            headers=dict(response.headers),
            text=response.text,
            url=response.url,
        )

    def delete(self, path: str, **kwargs: Any) -> HttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("DELETE %s", url)
        response = self.session.delete(
            url,
            timeout=self.timeout_seconds,
            allow_redirects=False,
            **kwargs,
        )
        return HttpResult(
            status_code=response.status_code,
            headers=dict(response.headers),
            text=response.text,
            url=response.url,
            _response=response,
        )

    def get_bytes(self, path: str, **kwargs: Any) -> BinaryHttpResult:
        url = urljoin(self.base_url, path.lstrip("/"))
        logger.debug("GET(bytes) %s", url)
        response = self.session.get(
            url,
            timeout=self.timeout_seconds,
            allow_redirects=False,
            **kwargs,
        )
        return BinaryHttpResult(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
            url=response.url,
        )
