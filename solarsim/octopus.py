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
    try:
        body = r.json()
    except ValueError:
        body = {}
    if body.get("errors"):
        raise ValueError(body["errors"][0].get("message", "Octopus GraphQL request failed"))
    r.raise_for_status()
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


_DEVICES_LIST_QUERY = "query($a: String!) { devices(accountNumber: $a) { id name deviceType integrationDeviceId } }"
_COMPLETED_QUERY = "query($a: String!) { completedDispatches(accountNumber: $a) { start end delta meta { location } } }"
_FLEX_PLANNED_QUERY = "query($d: String!) { flexPlannedDispatches(deviceId: $d) { start end energyAddedKwh type } }"


def _token(api_key: str) -> str:
    return _graphql(_TOKEN_QUERY, {"key": api_key})["obtainKrakenToken"]["token"]


def fetch_devices(api_key: str, account: str) -> list[dict]:
    """SmartFlex devices (chargers, vehicles, batteries) registered on the account."""
    return _graphql(_DEVICES_LIST_QUERY, {"a": account}, _token(api_key))["devices"] or []


def fetch_completed_dispatches(api_key: str, account: str) -> list[dict]:
    """Intelligent Octopus slots in which the car was actually dispatched; Octopus only returns about the last 12 hours."""
    return _graphql(_COMPLETED_QUERY, {"a": account}, _token(api_key))["completedDispatches"] or []


def fetch_planned_dispatches(api_key: str, account: str) -> dict[str, list[dict]]:
    """Upcoming dispatches keyed by device id. Octopus limits this call to 5 a minute."""
    token = _token(api_key)
    devices = _graphql(_DEVICES_LIST_QUERY, {"a": account}, token)["devices"] or []
    return {d["id"]: _graphql(_FLEX_PLANNED_QUERY, {"d": d["id"]}, token)["flexPlannedDispatches"] or []
            for d in devices}


_SESSIONS_QUERY = """query($a: String!, $after: DateTime) {
  devices(accountNumber: $a) { id name deviceType
    ... on SmartFlexVehicle { chargingSessions(after: $after, first: 100) { edges { node {
      start end ... on SmartFlexChargingSession { stateOfChargeChange stateOfChargeFinal energyAdded { value unit }
        dispatches { start end energyAddedKwh type } } } } } } } }"""


def fetch_charging_sessions(api_key: str, account: str, after: str) -> list[dict]:
    """Plugged-in charging sessions per vehicle from SmartFlex: vehicle name, start, end and kWh added."""
    devices = _graphql(_SESSIONS_QUERY, {"a": account, "after": after}, _token(api_key))["devices"] or []
    return [{"vehicle": d["name"], "device_id": d["id"], "start": n["start"], "end": n["end"],
             "kwh": float((n.get("energyAdded") or {}).get("value") or 0)}
            for d in devices for e in (d.get("chargingSessions") or {}).get("edges", []) for n in [e["node"]]]


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
