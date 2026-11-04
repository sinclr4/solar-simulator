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


GRAPHQL_URL = f"{BASE_URL}/v1/graphql/"

_TOKEN_QUERY = "mutation($key: String!) { obtainKrakenToken(input: {APIKey: $key}) { token } }"
_DEVICES_QUERY = """query($account: String!) {
  account(accountNumber: $account) {
    electricityAgreements(active: true) {
      meterPoint { mpan meters(includeInactive: false) { smartDevices { deviceId } } }
    }
  }
}"""
_TELEMETRY_QUERY = """query($deviceId: String!, $start: DateTime, $end: DateTime, $grouping: TelemetryGrouping) {
  smartMeterTelemetry(deviceId: $deviceId, start: $start, end: $end, grouping: $grouping) {
    readAt consumptionDelta
  }
}"""


def _graphql(query: str, variables: dict, token: str | None = None) -> dict:
    r = requests.post(GRAPHQL_URL, json={"query": query, "variables": variables},
                      headers={"Authorization": token} if token else None, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise ValueError(body["errors"][0].get("message", "Octopus GraphQL request failed"))
    return body["data"]


_PROPERTY_QUERY = "query($account: String!) { account(accountNumber: $account) { properties { id } } }"
_EV_QUERY = """query($p: ID!, $s: DateTime, $e: DateTime, $after: String) {
  property(id: $p) {
    measurements(startAt: $s, endAt: $e, timezone: "Europe/London", first: 1000, after: $after,
      utilityFilters: [{electricityFilters: {readingFrequencyType: THIRTY_MIN_INTERVAL,
        readingDirection: CONSUMPTION, modelledSubMeterReadings: true}}]) {
      pageInfo { hasNextPage endCursor }
      edges { node { ... on IntervalMeasurementType { value startAt } } }
    }
  }
}"""


def fetch_ev_readings(api_key: str, account: str, start: str, end: str) -> dict[str, float]:
    """Octopus's modelled EV-only half-hourly usage (kWh) keyed by UTC slot start; slots without EV use are absent."""
    token = _graphql(_TOKEN_QUERY, {"key": api_key})["obtainKrakenToken"]["token"]
    properties = _graphql(_PROPERTY_QUERY, {"account": account}, token)["account"]["properties"]
    readings: dict[str, float] = {}
    for prop in properties:
        after = None
        while True:
            page = _graphql(_EV_QUERY, {"p": prop["id"], "s": start, "e": end, "after": after},
                            token)["property"]["measurements"]
            for e in page["edges"]:
                n = e["node"]
                if n.get("startAt") and n.get("value") is not None:
                    readings[n["startAt"]] = readings.get(n["startAt"], 0.0) + float(n["value"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
    return readings


def fetch_live_consumption(api_key: str, account: str, mpan: str, start: str, end: str) -> list[dict]:
    """Half-hourly smart meter telemetry as REST-style rows (kWh); empty if the meter has no live data."""
    token = _graphql(_TOKEN_QUERY, {"key": api_key})["obtainKrakenToken"]["token"]
    agreements = _graphql(_DEVICES_QUERY, {"account": account}, token)["account"]["electricityAgreements"]
    device_ids = [d["deviceId"] for a in agreements if a["meterPoint"]["mpan"] == mpan
                  for m in a["meterPoint"]["meters"] for d in m["smartDevices"] or [] if d.get("deviceId")]
    if not device_ids:
        return []
    data = _graphql(_TELEMETRY_QUERY, {"deviceId": device_ids[0], "start": start, "end": end,
                                       "grouping": "HALF_HOURLY"}, token)
    return [{"interval_start": t["readAt"], "consumption": float(t["consumptionDelta"]) / 1000}
            for t in data["smartMeterTelemetry"] or [] if t["consumptionDelta"] is not None]


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
