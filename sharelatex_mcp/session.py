from __future__ import annotations

import logging
import re

from sharelatex_mcp.config import AppConfig
from sharelatex_mcp.http import HttpClient

logger = logging.getLogger(__name__)


def _extract_csrf(html: str) -> str:
    hidden_match = re.search(r'name="_csrf"\s+type="hidden"\s+value="([^"]+)"', html, flags=re.IGNORECASE)
    if hidden_match:
        return hidden_match.group(1)

    meta_match = re.search(r'<meta\s+name="ol-csrfToken"\s+content="([^"]+)"', html, flags=re.IGNORECASE)
    if meta_match:
        return meta_match.group(1)

    raise RuntimeError("Unable to parse CSRF token from login page")


class OverleafSessionManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.http = HttpClient(config.base_url, config.timeout_seconds)
        self._csrf_token: str | None = None

    def login(self) -> None:
        logger.info("Attempting login to %s", self.config.base_url)
        login_page = self.http.get("/login")
        if login_page.status_code != 200:
            raise RuntimeError(f"Failed to access login page, status code: {login_page.status_code}")

        csrf_token = _extract_csrf(login_page.text)
        self._csrf_token = csrf_token
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

        logger.info("Login successful")

    def ensure_logged_in(self) -> None:
        if self.is_logged_in():
            return
        self.login()
        if not self.is_logged_in():
            raise RuntimeError("No valid session established after login")

    def is_logged_in(self) -> bool:
        home = self.http.get("/project")
        location = home.headers.get("Location", "")
        return not (300 <= home.status_code < 400 and "/login" in location)

    def get_csrf_token(self, project_id: str | None = None, force_refresh: bool = False) -> str:
        if self._csrf_token and not force_refresh:
            return self._csrf_token

        self.ensure_logged_in()

        if not force_refresh and self._csrf_token:
            return self._csrf_token

        path = f"/project/{project_id}" if project_id else "/project"
        logger.debug("Fetching CSRF token from %s", path)
        project_page = self.http.get(path)
        if project_page.status_code >= 400:
            raise RuntimeError(f"Failed to read CSRF page, status code: {project_page.status_code}")
        self._csrf_token = _extract_csrf(project_page.text)
        return self._csrf_token
