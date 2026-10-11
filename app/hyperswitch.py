"""Thin Hyperswitch REST client. The secret API key lives only here.

Two kinds of failure, handled differently by callers:
  HyperswitchError        Hyperswitch answered with an error body (4xx): the
                          outcome is KNOWN (e.g. HE_01 duplicate, IR_14 bad state).
  HyperswitchUnavailable  timeout / network error / 5xx: the outcome is UNKNOWN.
                          Never blindly retry a money-moving call; GET and reconcile.
"""
import httpx

from app.config import settings


class HyperswitchError(Exception):
    def __init__(self, http_status: int, code: str | None, message: str, body: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.http_status = http_status
        self.code = code
        self.message = message
        self.body = body or {}


class HyperswitchUnavailable(Exception):
    pass


_client = httpx.AsyncClient(
    base_url=settings.hyperswitch_base_url,
    headers={
        "api-key": settings.hyperswitch_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    },
    timeout=httpx.Timeout(20.0, connect=10.0),
)


async def _request(method: str, path: str, json: dict | None = None) -> dict:
    try:
        resp = await _client.request(method, path, json=json)
    except httpx.HTTPError as exc:
        raise HyperswitchUnavailable(f"{method} {path}: {exc!r}") from exc

    if resp.status_code >= 500:
        raise HyperswitchUnavailable(f"{method} {path}: HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise HyperswitchUnavailable(f"{method} {path}: non-JSON HTTP {resp.status_code}")
    if resp.status_code >= 400:
        err = body.get("error") or {}
        raise HyperswitchError(resp.status_code, err.get("code"), err.get("message", "Unknown error"), body)
    return body


async def create_payment(payload: dict) -> dict:
    return await _request("POST", "/payments", payload)


async def retrieve_payment(payment_id: str) -> dict:
    return await _request("GET", f"/payments/{payment_id}")


async def cancel_payment(payment_id: str, reason: str) -> dict:
    return await _request("POST", f"/payments/{payment_id}/cancel", {"cancellation_reason": reason})


async def close() -> None:
    await _client.aclose()
