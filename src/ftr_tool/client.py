from __future__ import annotations

import base64
import json
import logging
import ssl
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import JamaError, ValidationError


@dataclass(frozen=True)
class Project:
    id: int
    key: str
    name: str


class JamaClient:
    """Small Jama REST v1 client using only the Python standard library."""

    def __init__(self, base_url: str, logger: logging.Logger | None = None, timeout: int = 45):
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            raise ValidationError("URL must use http:// or https:// format.")
        self.base_url = base_url
        self.rest_url = f"{base_url}/rest/v1"
        self.timeout = timeout
        self.logger = logger or logging.getLogger("ftr_tool")
        self._authorization: str | None = None
        self._ssl_context = ssl.create_default_context()

    def authenticate_basic(self, username: str, password: str) -> None:
        if not username or not password:
            raise ValidationError("Username and password are required for Basic authentication.")
        encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._authorization = f"Basic {encoded}"
        self.list_projects()

    def authenticate_oauth(self, client_id: str, client_secret: str) -> None:
        if not client_id or not client_secret:
            raise ValidationError("Client ID and client secret are required for OAuth.")
        encoded = base64.b64encode(f"{client_id}:{client_secret}".encode("ascii")).decode("ascii")
        body = urlencode({"grant_type": "client_credentials"}).encode("ascii")
        payload = self._request_url(
            f"{self.base_url}/rest/oauth/token",
            method="POST",
            body=body,
            headers={
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise JamaError("OAuth response did not include an access token.")
        self._authorization = f"Bearer {token}"
        self.list_projects()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        data: Any | None = None,
    ) -> Any:
        if not self._authorization:
            raise JamaError("Authenticate before requesting Jama data.")
        url = f"{self.rest_url}/{path.lstrip('/')}"
        if query:
            clean = {key: value for key, value in query.items() if value is not None}
            url += "?" + urlencode(clean, doseq=True)
        body = None if data is None else json.dumps(data).encode("utf-8")
        headers = {"Authorization": self._authorization, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        payload = self._request_url(url, method=method, body=body, headers=headers)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def request_first(self, paths: Iterable[str], **kwargs: Any) -> Any:
        errors: list[str] = []
        for path in paths:
            try:
                return self.request(path, **kwargs)
            except JamaError as exc:
                errors.append(f"{path}: {exc}")
        raise JamaError("No supported Jama endpoint succeeded. " + " | ".join(errors))

    def paged(self, path: str, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        start_at = 0
        while True:
            page_query = dict(query or {})
            page_query.update({"startAt": start_at, "maxResults": 50})
            envelope = self._request_envelope(path, query=page_query)
            data = envelope.get("data", [])
            results.extend(data)
            page = envelope.get("meta", {}).get("pageInfo", {})
            count = int(page.get("resultCount", len(data)))
            total = int(page.get("totalResults", len(results)))
            if count == 0 or len(results) >= total:
                return results
            start_at += count

    def list_projects(self) -> list[Project]:
        data = self.paged("projects")
        return [
            Project(int(row["id"]), str(row.get("projectKey", "")), str(row.get("fields", {}).get("name", "")))
            for row in data
            if not row.get("isFolder")
        ]

    def _request_envelope(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._authorization:
            raise JamaError("Authenticate before requesting Jama data.")
        url = f"{self.rest_url}/{path.lstrip('/')}"
        if query:
            url += "?" + urlencode(query, doseq=True)
        result = self._request_url(url, headers={"Authorization": self._authorization, "Accept": "application/json"})
        if not isinstance(result, dict):
            raise JamaError("Jama returned an unexpected response.")
        return result

    def _request_url(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        safe_url = url.split("?", 1)[0]
        self.logger.info("%s %s", method, safe_url)
        request = Request(url, method=method, data=body, headers=headers or {})
        try:
            with urlopen(request, timeout=self.timeout, context=self._ssl_context) as response:
                content = response.read().decode("utf-8")
                return json.loads(content) if content else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
                message = parsed.get("meta", {}).get("message") or parsed.get("error_description") or raw
            except json.JSONDecodeError:
                message = raw or exc.reason
            self.logger.error("%s %s failed (%s): %s", method, safe_url, exc.code, message)
            raise JamaError(f"Jama returned HTTP {exc.code}: {message}") from exc
        except URLError as exc:
            self.logger.error("%s %s failed: %s", method, safe_url, exc.reason)
            raise JamaError(f"Could not connect to Jama: {exc.reason}") from exc

