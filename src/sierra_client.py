"""
Sierra GraphQL client — cookie + CSRF auth, POST as text/plain, retry/backoff.

Sierra sends all API calls as anonymous POSTs to /-/api/graphql with
Content-Type: text/plain;charset=UTF-8 (unusual but confirmed via HAR).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src import config

logger = logging.getLogger(__name__)


class SierraGraphQLError(RuntimeError):
    """Raised when the GraphQL response has a non-empty `errors` array."""
    def __init__(self, errors: list[dict], query_name: str):
        self.errors = errors
        self.query_name = query_name
        msgs = "; ".join(e.get("message", str(e)) for e in errors)
        super().__init__(f"[{query_name}] {msgs}")


class SierraAuthError(RuntimeError):
    """Raised on 401/403 — cookie or CSRF likely expired."""


class SierraClient:
    def __init__(
        self,
        cookie: str = config.SIERRA_COOKIE,
        csrf_token: str = config.SIERRA_CSRF_TOKEN,
        user_agent: str = config.SIERRA_USER_AGENT,
        graphql_url: str = config.SIERRA_GRAPHQL_URL,
        agent_id: str = config.SIERRA_AGENT_ID,
        workspace_version_id: str = config.SIERRA_WORKSPACE_VERSION_ID,
        delay_seconds: float = config.REQUEST_DELAY_SECONDS,
    ) -> None:
        self.graphql_url = graphql_url
        self.agent_id = agent_id
        self.workspace_version_id = workspace_version_id
        self.delay = delay_seconds
        self._last_request_ts: float = 0.0

        self.session = requests.Session()
        self.session.verify = config.SSL_VERIFY
        if config.PROXIES:
            self.session.proxies.update(config.PROXIES)
        if not config.SSL_VERIFY:
            # Silence InsecureRequestWarning when verify is intentionally off.
            import urllib3  # type: ignore
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session.headers.update({
            "Accept":                   "*/*",
            "Accept-Language":          "en-US,en;q=0.9,es;q=0.8",
            "Content-Type":             "text/plain;charset=UTF-8",
            "Origin":                   config.SIERRA_BASE_URL,
            "Referer":                  f"{config.SIERRA_BASE_URL}/agents/"
                                        f"{agent_id.removeprefix('bot-')}/sessions/",
            "User-Agent":               user_agent,
            "x-sierra-csrf-token":      csrf_token,
            "Cookie":                   cookie,
            "sec-ch-ua":                '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile":         "?0",
            "sec-ch-ua-platform":       '"Windows"',
            "sec-fetch-dest":           "empty",
            "sec-fetch-mode":           "cors",
            "sec-fetch-site":           "same-origin",
        })

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_ts = time.monotonic()

    @retry(
        retry=retry_if_exception_type((requests.RequestException,)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _post(self, body: dict) -> requests.Response:
        self._throttle()
        resp = self.session.post(
            self.graphql_url,
            data=json.dumps(body, separators=(",", ":")),
            timeout=60,
        )
        if resp.status_code in (401, 403):
            raise SierraAuthError(
                f"HTTP {resp.status_code} — cookie/CSRF likely expired. "
                f"Re-login to Sierra and update .env. Body: {resp.text[:200]}"
            )
        resp.raise_for_status()
        return resp

    def query(
        self,
        query_text: str,
        variables: dict[str, Any],
        *,
        operation_label: str = "graphql",
    ) -> dict:
        """Send a GraphQL query and return `data`. Raises on `errors`."""
        body = {"query": query_text, "variables": variables}
        resp = self._post(body)
        payload = resp.json()
        if payload.get("errors"):
            raise SierraGraphQLError(payload["errors"], operation_label)
        return payload.get("data", {})

    # ---- convenience helpers bound to our agent ------------------------------

    def query_bot(self, query_text: str, variables: dict, **kw) -> dict:
        """Inject agentId/botId into variables if absent."""
        v = dict(variables)
        for k in ("agentId", "botId"):
            v.setdefault(k, self.agent_id)
        return self.query(query_text, v, **kw)

    def query_workspace(self, query_text: str, variables: dict, **kw) -> dict:
        v = dict(variables)
        v.setdefault("workspaceVersionId", self.workspace_version_id)
        return self.query(query_text, v, **kw)
