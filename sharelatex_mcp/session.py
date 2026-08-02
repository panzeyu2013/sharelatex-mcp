from __future__ import annotations

import logging
import threading
import time

import requests
from bs4 import BeautifulSoup

from sharelatex_mcp.config import AppConfig
from sharelatex_mcp.http import HttpClient
from sharelatex_mcp.validation import validate_project_id

logger = logging.getLogger(__name__)

# How long a *failed* login-status probe is cached before re-checking.  Success
# results are cached for the full ``session_check_ttl_seconds`` window; failures
# must be re-checked quickly so the login flow never loops on a stale "False".
_LOGIN_RECHECK_AFTER_FAILURE_SECONDS = 2.0


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
        # Login-status check cache: avoids probing /project on every operation,
        # which trips the instance rate limiter (HTTP 429) and cascades into
        # spurious re-logins.  ``_login_check_ok`` starts False so the first
        # check always performs a real probe and establishes the session.
        self._login_check_at: float = 0.0
        self._login_check_ok: bool = False
        self._login_check_ttl: float = float(config.session_check_ttl_seconds)
        # Serializes login / CSRF / cookie mutations so concurrent background
        # job workers and foreground tool calls never race on the shared
        # requests.Session state.
        self._state_lock = threading.RLock()

    def close(self) -> None:
        self.http.close()

    def invalidate_login(self) -> None:
        with self._state_lock:
            self._csrf_token = None
            self._login_check_at = 0.0
            self._login_check_ok = False
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
        # Drop the cached "not logged in" result so the post-login probe in
        # ensure_logged_in re-checks rather than reading the stale value.
        self._login_check_at = 0.0
        self._login_check_ok = False
        logger.info("Login successful")

    def ensure_logged_in(self) -> None:
        if self.is_logged_in():
            return
        self.login()
        if not self.is_logged_in():
            raise RuntimeError("No valid session established after login")

    def is_logged_in(self) -> bool:
        # Short-TTL cache: within the window we return the last result without
        # another HTTP round-trip, so read/write don't hammer /project.  Only a
        # *successful* (logged-in) result is cached for the full TTL; a failed
        # probe is cached very briefly so the login flow re-checks promptly
        # instead of looping re-logins on a stale "False".
        now = time.monotonic()
        check_at = getattr(self, "_login_check_at", 0.0)
        ok = bool(getattr(self, "_login_check_ok", False))
        ttl = float(getattr(self, "_login_check_ttl", 30.0))
        effective_ttl = ttl if ok else _LOGIN_RECHECK_AFTER_FAILURE_SECONDS
        if now - check_at < effective_ttl:
            return ok
        try:
            home = self.http.get("/project")
        except (requests.ConnectionError, requests.Timeout, RuntimeError):
            logger.debug("Network error checking login status", exc_info=True)
            self._login_check_at = now
            self._login_check_ok = False
            return False
        if home.status_code == 429:
            # Rate limited — assume the session is still valid rather than
            # treating the throttle as a logout and cascading into a re-login.
            # Caching True also lets the limiter cool down for one TTL.
            logger.debug("Login-status check rate limited (429); assuming logged in")
            self._login_check_at = now
            self._login_check_ok = True
            return True
        self._login_check_at = now
        self._login_check_ok = 200 <= home.status_code < 300
        return self._login_check_ok

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
