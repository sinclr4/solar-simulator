from __future__ import annotations

import requests

# Fixed on purpose: users supply an API key and account number, never a URL.
BASE_URL = "https://api.octopus.energy"


def _account(api_key: str, account: str) -> dict:
    r = requests.get(f"{BASE_URL}/v1/accounts/{account}/", auth=(api_key, ""), timeout=60)
    if r.status_code in (401, 403, 404):
        raise ValueError("Octopus rejected that API key / account number - please check them")
    r.raise_for_status()
    return r.json()


def import_meter_points(api_key: str, account: str) -> list[dict]:
    points = [mp for p in _account(api_key, account)["properties"] for mp in p["electricity_meter_points"]
              if not mp.get("is_export")]
    return points


def discover(api_key: str, account: str) -> str:
    """Return the MPAN of the account's electricity import meter point."""
    points = import_meter_points(api_key, account)
    if not points:
        raise ValueError("No electricity meter was found on that Octopus account")
    return points[0]["mpan"]


def fetch_meter_serials(api_key: str, account: str, mpan: str) -> list[str]:
    return [m["serial_number"] for mp in import_meter_points(api_key, account)
            if mp["mpan"] == mpan for m in mp["meters"]]


def fetch_consumption(api_key: str, mpan: str, serial: str, period_from: str, period_to: str) -> list[dict]:
    url = f"{BASE_URL}/v1/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
    params = {"period_from": period_from, "period_to": period_to,
              "page_size": 25000, "order_by": "period"}
    rows: list[dict] = []
    while url:
        r = requests.get(url, params=params, auth=(api_key, ""), timeout=60)
        r.raise_for_status()
        data = r.json()
        rows.extend(data["results"])
        url, params = data.get("next"), None
    return rows
