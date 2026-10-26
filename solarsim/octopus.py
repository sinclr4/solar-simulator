from __future__ import annotations

import requests


def fetch_meter_serials(base_url: str, api_key: str, account: str, mpan: str) -> list[str]:
    r = requests.get(f"{base_url}/v1/accounts/{account}/", auth=(api_key, ""), timeout=60)
    r.raise_for_status()
    return [m["serial_number"] for p in r.json()["properties"] for mp in p["electricity_meter_points"]
            if mp["mpan"] == mpan for m in mp["meters"]]


def fetch_consumption(base_url: str, api_key: str, mpan: str, serial: str,
                      period_from: str, period_to: str) -> list[dict]:
    url = f"{base_url}/v1/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
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
