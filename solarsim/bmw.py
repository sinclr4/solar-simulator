from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests

# BMW CarData customer API: OAuth device-code login, then REST calls with the access token.
AUTH_URL = "https://customer.bmwgroup.com/gcdm/oauth"
API_URL = "https://api-cardata.bmwgroup.com"
SCOPE = "authenticate_user openid cardata:api:read"


def _post_form(url: str, data: dict) -> requests.Response:
    return requests.post(url, data=data, headers={"Accept": "application/json"}, timeout=30)


def _error(r: requests.Response) -> str:
    try:
        body = r.json()
        return body.get("error_description") or body.get("error") or body.get("exveErrorMsg") or r.text[:200]
    except ValueError:
        return r.text[:200] or f"HTTP {r.status_code}"


def start_device_flow(client_id: str) -> dict:
    """Ask BMW for a one-time code the user enters in their browser; returns the codes plus the PKCE verifier."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = _post_form(f"{AUTH_URL}/device/code", {
        "client_id": client_id, "response_type": "device_code", "scope": SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    if not r.ok:
        raise ValueError(f"BMW rejected the login request: {_error(r)}")
    return {**r.json(), "code_verifier": verifier}


def wait_for_tokens(client_id: str, flow: dict) -> dict:
    """Poll until the user has approved the device in their browser."""
    interval = int(flow.get("interval") or 5)
    deadline = time.time() + int(flow.get("expires_in") or 300)
    while time.time() < deadline:
        time.sleep(interval)
        r = _post_form(f"{AUTH_URL}/token", {
            "client_id": client_id, "device_code": flow["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "code_verifier": flow["code_verifier"]})
        if r.ok:
            return r.json()
        error = r.json().get("error") if r.headers.get("content-type", "").startswith("application/json") else None
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise ValueError(f"BMW login failed: {_error(r)}")
    raise ValueError("The login code expired - run the command again")


def refresh_tokens(client_id: str, refresh_token: str) -> dict:
    r = _post_form(f"{AUTH_URL}/token", {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                         "client_id": client_id})
    if not r.ok:
        raise ValueError(f"BMW login has expired - run bmw-login again ({_error(r)})")
    return r.json()


def _get(path: str, token: str, params: dict | None = None):
    r = requests.get(f"{API_URL}{path}", params=params, timeout=60,
                     headers={"Authorization": f"Bearer {token}", "x-version": "v1"})
    if r.status_code == 429:
        raise ValueError("BMW CarData daily limit (50 requests) reached - try again after 00:00 UTC")
    if not r.ok:
        raise ValueError(f"BMW CarData request failed ({r.status_code}): {_error(r)}")
    return r.json()


def mapped_vins(token: str) -> list[str]:
    data = _get("/customers/vehicles/mappings", token)
    items = data if isinstance(data, list) else data.get("vehicles") or data.get("data") or [data]
    return [v["vin"] for v in items if isinstance(v, dict) and v.get("vin")]


def session_slot_kwh(session: dict) -> dict[int, float]:
    """Grid energy (kWh) per half-hour for one session, keyed by the slot's UTC epoch start."""
    blocks = [(b["startTime"], b["endTime"], b["averagePowerGridKw"] / 3600) for b in session.get("chargingBlocks") or []]
    kwh, start, end = session.get("energyConsumedFromPowerGridKwh") or 0.0, session.get("startTime"), session.get("endTime")
    if not blocks and kwh > 0 and start and end and end > start:
        blocks = [(start, end, kwh / (end - start))]  # no power readings, so assume steady charging
    out: dict[int, float] = defaultdict(float)
    for t0, t1, kwh_per_s in blocks:
        t = t0
        while t < t1:
            slot = t - t % 1800
            nxt = min(t1, slot + 1800)
            out[slot] += kwh_per_s * (nxt - t)
            t = nxt
    return dict(out)


def charging_sessions(token: str, vin: str, start: datetime, end: datetime) -> list[dict]:
    """Charging sessions between two timezone-aware datetimes; BMW rejects ranges over about a month, so ask in chunks."""
    rows: list[dict] = []
    lo = start
    while lo < end:
        hi = min(lo + timedelta(days=30), end)
        next_token = None
        while True:
            params = {"from": lo.isoformat(), "to": hi.isoformat()}
            if next_token:
                params["nextToken"] = next_token
            page = _get(f"/customers/vehicles/{vin}/chargingHistory", token, params)
            rows += page.get("data") or []
            next_token = page.get("next_token")
            if not next_token:
                break
        lo = hi
    return rows
