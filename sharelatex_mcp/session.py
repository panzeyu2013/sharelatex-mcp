from __future__ import annotations

import logging
import threading

import requests
from bs4 import BeautifulSoup

from sharelatex_mcp.config import AppConfig
from sharelatex_mcp.http import HttpClient
from sharelatex_mcp.validation import validate_project_id

logger = logging.getLogger(__name__)


def _extract_csrf(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    hidden = soup.find("input", attrs={"name": "_csrf", "type": "hidden"})
    if hidden and hidden.get("value"):
        return str(hidden["value"])

    meta = soup.find("meta", attrs={"name": "ol-csrfToken"})
    if meta and meta.get("content"):
        return str(meta["content"])

    raise RuntimeError("Unable to parse CSRF token from login page")


class OverleafSessionManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.http = HttpClient(config.base_url, config.timeout_seconds)
        self._csrf_token: str | None = None
        # Serializes login / CSRF / cookie mutations so concurrent background
        # job workers and foreground tool calls never race on the shared
        # requests.Session state.
        self._state_lock = threading.RLock()

    def close(self) -> None:
        self.http.close()

    def invalidate_login(self) -> None:
        with self._state_lock:
            self._csrf_token = None
            self.http.session.cookies.clear()

    def login(self) -> None:
        with self._state_lock:
            self._login_locked()

    def _login_locked(self) -> None:
        logger.info("Attempting login to %s", self.config.base_url)
        login_page = self.http.get("/login")
        if login_page.status_code != 200:
            raise RuntimeError(f"Failed to access login page, status code: {login_page.status_code}")

        csrf_token = _extract_csrf(login_page.text)
        logger.debug("Extracted CSRF token from login page")

        login_result = self.http.post_form(
            "/login",
            data={
                "_csrf": csrf_token,
                "email": self.config.email,
                "password": self.config.password,
            },
            headers={"Referer": f"{self.config.base_url}/login"},
        )

        location = login_result.headers.get("Location", "")
        if login_result.status_code == 200 and "Your email or password is incorrect" in login_result.text:
            raise RuntimeError("Login failed: incorrect email or password")
        if login_result.status_code >= 400:
            raise RuntimeError(f"Login request failed, status code: {login_result.status_code}")
        if "/login" in location:
            raise RuntimeError("Still redirected to login page after authentication")

        self._csrf_token = csrf_token
        logger.info("Login successful")

    def ensure_logged_in(self) -> None:
        if self.is_logged_in():
            return
        self.login()
        if not self.is_logged_in():
            raise RuntimeError("No valid session established after login")

    def is_logged_in(self) -> bool:
        try:
            home = self.http.get("/project")
        except (requests.ConnectionError, requests.Timeout, RuntimeError):
            logger.debug("Network error checking login status", exc_info=True)
            return False
        return 200 <= home.status_code < 300

    def get_csrf_token(self, project_id: str | None = None, force_refresh: bool = False) -> str:
        if project_id is not None:
            project_id = validate_project_id(project_id)

        with self._state_lock:
            if self._csrf_token and not force_refresh:
                return self._csrf_token

            self.ensure_logged_in()

            if not force_refresh and self._csrf_token:
                return self._csrf_token

            path = f"/project/{project_id}" if project_id else "/project"
            logger.debug("Fetching CSRF token from %s", path)
            project_page = self.http.get(path)
            if not 200 <= project_page.status_code < 300:
                raise RuntimeError(f"Failed to read CSRF page, status code: {project_page.status_code}")
            self._csrf_token = _extract_csrf(project_page.text)
            return self._csrf_token
