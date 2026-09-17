"""Bluesky access through the atproto SDK."""

import asyncio
import json
import re
import time
from typing import Any, cast
from urllib.parse import unquote, urlparse

import httpx
from atproto import AsyncClient
from atproto.exceptions import AtProtocolError, RateLimitExceededError, RequestErrorBase
from atproto_client.models.dot_dict import DotDict
from atproto_client.request import AsyncRequest

PUBLIC_API = "https://public.api.bsky.app"
UNAVAILABLE = {
    "ActorNotFound",
    "RepoNotFound",
    "AccountTakedown",
    "AccountDeactivated",
    "BlockedActor",
    "BlockedByActor",
    "RepoDeactivated",
    "RepoTakendown",
    "RepoSuspended",
}


class APIError(Exception):
    def __init__(
        self, message: str, *, code: str = "", status: int = 0, invalid_cursor: bool = False
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.invalid_cursor = invalid_cursor

    @property
    def unavailable(self):
        return self.code in UNAVAILABLE


class RetryingRequest(AsyncRequest):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pause_until = 0.0

    async def _send_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(5):
            while (delay := self.pause_until - time.monotonic()) > 0:
                if delay > 300:
                    raise APIError(
                        f"Server requested a {delay:.0f}-second pause. Run prism resume later",
                        code="RateLimitPause",
                        status=429,
                    )
                await asyncio.sleep(min(delay, 30))
            try:
                return await super()._send_request(method, url, follow_redirects=False, **kwargs)
            except (RequestErrorBase, httpx.TransportError) as error:
                response = getattr(error, "response", None)
                status = response.status_code if response else 0
                retryable = status == 429 or (method == "GET" and (status == 0 or status >= 500))
                if retryable:
                    rate_limit = RateLimitExceededError(response)
                    delay = rate_limit.retry_after
                    if delay is None and rate_limit.reset_at is not None:
                        delay = rate_limit.reset_at.timestamp() - time.time()
                    delay = max(1, delay if delay is not None else 2**attempt)
                    self.pause_until = max(self.pause_until, time.monotonic() + delay)
                    if delay > 300:
                        raise APIError(
                            f"Server requested a {delay:.0f}-second pause. Run prism resume later",
                            code="RateLimitPause",
                            status=429,
                        ) from error
                    if attempt < 4:
                        continue
                body = response.content if response else None
                code = getattr(body, "error", "HTTPError")
                # Server messages can echo passwords. Show only the error code.
                if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z]{1,64}", code):
                    code = "HTTPError"
                invalid_cursor = code == "InvalidCursor" or (
                    code == "InvalidRequest"
                    and "cursor" in str(getattr(body, "message", "")).lower()
                )
                raise APIError(
                    f"{status} {urlparse(url).path}: {code}",
                    code=code,
                    status=status,
                    invalid_cursor=invalid_cursor,
                ) from error
        raise AssertionError("Retry loop exited without a response")


class Bluesky(AsyncClient):
    def __init__(self, request: AsyncRequest):
        super().__init__(base_url=PUBLIC_API, request=request)

    async def get(self, method: str, **params: Any) -> dict[str, Any]:
        response = await self.invoke_query(method, params=cast(Any, DotDict(params)))
        return self._object(response.content)

    @staticmethod
    def _object(content: object) -> dict[str, Any]:
        if not isinstance(content, dict):
            raise APIError("The API response was not a JSON object")
        return content

    async def pds(self, did: str) -> str:
        # Keep support for did:web paths, which the SDK resolver does not support.
        if did.startswith("did:plc:"):
            url = f"https://plc.directory/{did}"
        elif did.startswith("did:web:"):
            parts = did.removeprefix("did:web:").split(":")
            host = unquote(parts[0])
            path = "/".join(parts[1:]) if len(parts) > 1 else ".well-known"
            url = f"https://{host}/{path}/did.json"
        else:
            raise APIError(f"Unsupported DID method: {did}")
        response = await self.request.get(url)
        content = response.content
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if isinstance(content, bytes) and content_type in {
            "application/did+json",
            "application/did+ld+json",
        }:
            try:
                content = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise APIError("The API response was not valid JSON") from error
        document = self._object(content)
        for service in document.get("service", []):
            if service.get("id", "").endswith("#atproto_pds"):
                endpoint = service.get("serviceEndpoint", "")
                parsed = urlparse(endpoint)
                if parsed.scheme == "https" and parsed.netloc and not parsed.username:
                    return endpoint.rstrip("/")
        raise APIError("The account DID document has no HTTPS PDS endpoint")

    async def authenticate(self, pds: str, did: str, password: str) -> None:
        def check_account(event: object, session: Any) -> None:
            if session.did != did:
                raise APIError("The app password authenticated a different account")

        self.update_base_url(pds)
        self.on_session_change(check_account)
        try:
            await super().login(did, password, fetch_bsky_profile=False)
        except (AtProtocolError, ValueError) as error:
            raise APIError("The API returned an invalid login response") from error

    async def repo(
        self,
        pds: str,
        method: str,
        *,
        data: dict[str, Any] | None = None,
        blob: bytes | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        if data is not None and blob is not None:
            raise ValueError("A repository request cannot contain both JSON and image bytes")
        self.update_base_url(pds)
        if blob is not None:
            response = await self.invoke_procedure(method, data=blob, input_encoding="image/png")
        elif data is not None:
            response = await self.invoke_procedure(
                method, data=cast(Any, DotDict(data)), input_encoding="application/json"
            )
        else:
            response = await self.invoke_query(method, params=cast(Any, DotDict(params)))
        return self._object(response.content)
