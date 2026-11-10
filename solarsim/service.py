from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
from dateutil.relativedelta import relativedelta

from . import bmw, credentials, octopus, weather
from .model import LOCAL_TZ, SLOT_H, Strategy, System, Tariff, annualise, build_frame, dispatch, payback, simulate
from .store import Store

LOCAL_USER = "local"


def local_user() -> str:
    """The Cosmos user that local (not signed-in) runs act as; set LOCAL_USER_ID to reuse a deployed user's data."""
    return os.environ.get("LOCAL_USER_ID") or LOCAL_USER
ORIENTATIONS = {"east_west": "East / West", "south": "North / South (panels on south face)"}
QUOTE_FIELDS = ("name", "solar_kwp", "battery_kwh", "battery_kw", "round_trip_eff", "export_limit_kw", "cost_gbp")
MAX_QUOTES = 6
STRATEGIES = [
    Strategy("Solar + battery (charged from solar only)", grid_charge=False),
    Strategy("Solar + battery + off-peak grid charging", grid_charge=True),
    Strategy("Full off-peak charge, sell surplus at export rate", grid_charge=True, export_arbitrage=True),
]
TYPICAL_DAYS = 56
HISTORY_DAYS = 30  # days of dispatch the energy-flow chart can show
FORECAST_FLOWS = ("load", "pv", "solar_charge", "grid_charge", "discharge", "import", "export", "ev_solar")
FORECAST_TOTALS = ("load", "pv", "solar_charge", "direct", "discharge", "ev_solar", "import", "export", "grid_charge", "curtailed", "cost_before", "cost_after", "saving",
                   "bmw_direct", "bmw_discharge", "bmw_grid_house", "oev_direct", "oev_discharge", "oev_grid_house")


@dataclass
class Inputs:
    name: str = "Quote 1"
    days: int = 365
    solar_kwp: float = 7.76
    orientation: str = "east_west"
    east_share_pct: float = 60
    tilt: float = 35
    performance_ratio: float = 0.85
    typical_year: bool = False
    battery_kwh: float = 13.5
    battery_kw: float = 5.0
    round_trip_eff: float = 0.89
    export_limit_kw: float = 5.0
    cost_gbp: float = 13699
    peak_p: float = 34.96
    offpeak_p: float = 6.60
    export_p: float = 12.0
    price_rise_pct: float = 3.0
    degradation_pct: float = 0.5
    install_date: str = ""

    _bounds = {"days": (1, 1095), "solar_kwp": (0, 50), "east_share_pct": (0, 100), "tilt": (0, 90),
               "performance_ratio": (0.5, 1), "battery_kwh": (0, 100), "battery_kw": (0, 50),
               "round_trip_eff": (0.5, 1), "export_limit_kw": (0, 50), "cost_gbp": (0, 1e6),
               "peak_p": (0, 200), "offpeak_p": (0, 200), "export_p": (0, 200),
               "price_rise_pct": (-20, 50), "degradation_pct": (0, 10)}

    @classmethod
    def parse(cls, data: dict) -> "Inputs":
        vals = {}
        for f in fields(cls):
            if f.name not in data or f.name.startswith("_"):
                continue
            raw = data[f.name]
            if f.type == "bool":
                vals[f.name] = raw in (True, "true", "on", "1", 1)
            elif f.type in ("int", "float"):
                v = (int if f.type == "int" else float)(raw)
                lo, hi = cls._bounds[f.name]
                if not lo <= v <= hi:
                    raise ValueError(f"{f.name} must be between {lo} and {hi}")
                vals[f.name] = v
            else:
                vals[f.name] = str(raw).strip()[:60]
        inputs = cls(**vals)
        if inputs.orientation not in ORIENTATIONS:
            raise ValueError("orientation must be one of " + ", ".join(ORIENTATIONS))
        if inputs.install_date:
            pd.Timestamp(inputs.install_date)
        return inputs

    def faces(self) -> dict[str, float]:
        if self.orientation == "south":
            return {"south": 1.0}
        return {"east": self.east_share_pct / 100, "west": 1 - self.east_share_pct / 100}


def _window(days: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    end = pd.Timestamp.now(tz=LOCAL_TZ).normalize()
    return end - pd.Timedelta(days=days), end


def data_status(store: Store) -> dict:
    dates = store.dates("consumption")
    return {"days": len(dates), "first": dates[0] if dates else None, "last": dates[-1] if dates else None}


def list_quotes(store: Store) -> list[dict]:
    docs = [{k: d[k] for k in ("id", "saved_at", *QUOTE_FIELDS) if k in d} for d in store.list_all("quotes")]
    return sorted(docs, key=lambda q: q.get("saved_at", ""), reverse=True)


def save_quote(store: Store, data: dict) -> dict:
    inp = Inputs.parse({k: v for k, v in data.items() if k in QUOTE_FIELDS})
    quote_id = str(uuid.UUID(str(data["id"]))) if data.get("id") else str(uuid.uuid4())
    doc = {"id": quote_id, "saved_at": datetime.now(timezone.utc).isoformat(),
           **{k: getattr(inp, k) for k in QUOTE_FIELDS}}
    store.upsert("quotes", [doc])
    return doc


def delete_quote(store: Store, quote_id: str) -> None:
    store.delete("quotes", str(uuid.UUID(quote_id)))


OCTOPUS_DOC = "octopus"


def _octopus_details(store: Store) -> dict | None:
    doc = store.get("settings", OCTOPUS_DOC)
    # A developer running locally may keep their details in .env; never done for signed-in web users
    env = {k: os.environ.get(f"OCTOPUS_{k.upper()}") for k in ("api_key", "account", "mpan")}
    local_env = store.user_id == local_user() and all(env.values())
    if doc:
        try:
            return {"api_key": credentials.decrypt(doc["api_key_enc"]), "account": doc["account"], "mpan": doc["mpan"]}
        except ValueError:
            # The deployed app's encryption key isn't available locally
            if not local_env:
                raise
    return env if local_env else None


def octopus_status(store: Store) -> dict:
    doc = store.get("settings", OCTOPUS_DOC)
    if doc:
        return {"connected": True, "account": doc["account"], "mpan_ending": doc["mpan"][-4:]}
    env = _octopus_details(store)
    if env:
        return {"connected": True, "account": env["account"], "mpan_ending": env["mpan"][-4:]}
    return {"connected": False}


def save_octopus(store: Store, data: dict) -> dict:
    api_key = credentials.clean_api_key(data.get("api_key"))
    account = credentials.clean_account(data.get("account"))
    mpan = octopus.discover(api_key, account)
    store.upsert("settings", [{"id": OCTOPUS_DOC, "account": account, "mpan": mpan,
                               "api_key_enc": credentials.encrypt(api_key),
                               "saved_at": datetime.now(timezone.utc).isoformat()}])
    return octopus_status(store)


def delete_octopus(store: Store) -> None:
    store.delete("settings", OCTOPUS_DOC)


BMW_DOC = "bmw"
BMW_HISTORY_DAYS = 59  # BMW rejects a charging-history start older than about 60 days
BMW_SYNC_MINUTES = 60  # CarData allows only 50 requests a day, so don't re-sync more often than this


def _clean(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def bmw_connect(store: Store, client_id: str, refresh_token: str) -> None:
    old = _clean(store.get("settings", BMW_DOC) or {})
    store.upsert("settings", [{**old, "id": BMW_DOC, "client_id": client_id,
                               "refresh_token_enc": credentials.encrypt(refresh_token),
                               "saved_at": datetime.now(timezone.utc).isoformat()}])


def _bmw_update(store: Store, **changes) -> None:
    store.upsert("settings", [{**_clean(store.get("settings", BMW_DOC)), **changes}])


def _bmw_session(store: Store) -> tuple[str, dict]:
    """A fresh access token plus the BMW settings document."""
    doc = store.get("settings", BMW_DOC)
    if not doc:
        raise ValueError("Connect BMW first: python -m solarsim bmw-login --client-id <your CarData client ID>")
    old = credentials.decrypt(doc["refresh_token_enc"])
    tokens = bmw.refresh_tokens(doc["client_id"], old)
    # BMW issues a new refresh token on every refresh, so keep it or the login lapses after two weeks
    bmw_connect(store, doc["client_id"], tokens.get("refresh_token") or old)
    return tokens["access_token"], doc


def bmw_sessions(store: Store, days: int) -> list[dict]:
    token, doc = _bmw_session(store)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=min(days, BMW_HISTORY_DAYS))
    return [{**s, "vin": vin} for vin in doc.get("vins") or bmw.mapped_vins(token)
            for s in bmw.charging_sessions(token, vin, start, end)]


def _bmw_day_docs(sessions: list[dict], first_full_date: str) -> list[dict]:
    """One document per local day with BMW's half-hourly charging energy and that day's session summaries."""
    slots: dict[str, dict[str, float]] = {}
    infos: dict[str, list] = {}
    seen = set()
    for s in sessions:
        if (s["vin"], s["startTime"]) in seen:
            continue
        seen.add((s["vin"], s["startTime"]))
        t0 = pd.Timestamp(s["startTime"], unit="s", tz="UTC")
        infos.setdefault(t0.tz_convert(LOCAL_TZ).strftime("%Y-%m-%d"), []).append({
            "start": t0.isoformat(),
            "end": pd.Timestamp(s["endTime"], unit="s", tz="UTC").isoformat() if s.get("endTime") else None,
            "grid_kwh": round(s.get("energyConsumedFromPowerGridKwh") or 0.0, 2),
            "soc_start": s.get("displayedStartSoc"), "soc_end": s.get("displayedSoc"),
            "location": (s.get("chargingLocation") or {}).get("municipality"), "vin_ending": s["vin"][-4:]})
        for epoch, kwh in bmw.session_slot_kwh(s).items():
            t = pd.Timestamp(epoch, unit="s", tz="UTC")
            day = slots.setdefault(t.tz_convert(LOCAL_TZ).strftime("%Y-%m-%d"), {})
            day[t.isoformat()] = day.get(t.isoformat(), 0.0) + kwh
    return [{"id": f"bmw-{d}", "date": d, "kind": "bmw", "sessions": infos.get(d, []),
             "slots": [{"start": k, "kwh": round(v, 3)} for k, v in sorted(slots.get(d, {}).items())]}
            for d in sorted(set(slots) | set(infos)) if d >= first_full_date]


def sync_bmw(store: Store, force: bool = False) -> dict | None:
    """Save recent BMW charging sessions; None if BMW isn't connected or was synced within the last hour."""
    doc = store.get("settings", BMW_DOC)
    if not doc:
        return None
    now = datetime.now(timezone.utc)
    last = datetime.fromisoformat(doc["last_sync"]) if doc.get("last_sync") else None
    if last and not force and now - last < timedelta(minutes=BMW_SYNC_MINUTES):
        return None
    token, doc = _bmw_session(store)
    oldest = now - timedelta(days=BMW_HISTORY_DAYS)
    since = oldest if force or not last else max(last - timedelta(days=3), oldest)
    vins = doc.get("vins") or bmw.mapped_vins(token)
    sessions = [{**s, "vin": v} for v in vins for s in bmw.charging_sessions(token, v, since, now)]
    # The first day of a window is only partly covered, so leave whatever is already stored for it
    first_full = (pd.Timestamp(since).tz_convert(LOCAL_TZ).normalize() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    docs = _bmw_day_docs(sessions, first_full)
    store.upsert("settings", docs)
    continuous = last is not None and since <= last and doc.get("covered_from")
    covered_from = min(doc["covered_from"], first_full) if continuous else first_full
    _bmw_update(store, vins=vins, last_sync=now.isoformat(), covered_from=covered_from)
    return {"sessions": len(sessions), "days_saved": len(docs), "covered_from": covered_from}


def bmw_compare(store: Store) -> dict:
    """BMW's own charging energy against Octopus's modelled EV usage, per day and per half-hour."""
    doc = store.get("settings", BMW_DOC)
    if not doc or not doc.get("covered_from"):
        raise ValueError("No BMW data stored yet - run: python -m solarsim bmw-sync")
    first = doc["covered_from"]
    bmw_docs = [d for d in store.by_date("settings", first, "2100-01-01") if d.get("kind") == "bmw"]
    cons = store.by_date("consumption", first, "2100-01-01")
    ev = pd.Series({pd.Timestamp(s["start"]): s.get("ev", 0.0) for d in cons for s in d["slots"]}, dtype=float)
    b = pd.Series({pd.Timestamp(s["start"]): s["kwh"] for d in bmw_docs for s in d["slots"]}, dtype=float)
    idx = ev.index.union(b.index)
    df = pd.DataFrame({"bmw": b.reindex(idx).fillna(0.0), "octopus": ev.reindex(idx).fillna(0.0)})
    df = df[df.index.isin(ev.index)]  # only slots where Octopus data exists
    df["date"] = df.index.tz_convert(LOCAL_TZ).strftime("%Y-%m-%d")
    days = df.groupby("date")[["bmw", "octopus"]].sum().round(1)
    days = days[(days["bmw"] > 0) | (days["octopus"] > 0)]
    both = df[(df["bmw"] > 0.05) | (df["octopus"] > 0.05)]
    return {"days": days.reset_index().to_dict(orient="records"), "bmw_kwh": round(float(df["bmw"].sum()), 1),
            "octopus_kwh": round(float(df["octopus"].sum()), 1),
            "slots_compared": len(both), "slots_within_half_kwh": int(((both["bmw"] - both["octopus"]).abs() <= 0.5).sum()),
            "correlation": round(float(df["bmw"].corr(df["octopus"])), 3)}


def _live_rows(env: dict) -> tuple[pd.Timestamp, list[dict], str | None]:
    """Completed half-hours from live smart meter telemetry for the last two days and today."""
    live_start = _window(0)[1] - pd.Timedelta(days=2)
    now = pd.Timestamp.now(tz="UTC")
    try:
        live = octopus.fetch_live_consumption(env["api_key"], env["account"], env["mpan"],
                                              live_start.isoformat(), now.isoformat())
    except (requests.RequestException, ValueError) as e:
        return live_start, [], str(e)
    return live_start, [r for r in live if pd.Timestamp(r["interval_start"]) + pd.Timedelta(minutes=30) <= now], None


def _attach_ev(env: dict, rows: list[dict], since: pd.Timestamp) -> None:
    """Add Octopus's modelled EV usage to each half-hourly row from `since`; rows are left alone if it's unavailable."""
    try:
        readings = octopus.fetch_ev_readings(env["api_key"], env["account"], since.isoformat(),
                                             (pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=1)).isoformat())
    except (requests.RequestException, ValueError):
        return
    ev = {pd.Timestamp(k): v for k, v in readings.items()}
    for r in rows:
        t = pd.Timestamp(r["interval_start"])
        if t >= since:
            r["ev"] = round(min(ev.get(t, 0.0), r["consumption"]), 3)


def refresh_recent(store: Store) -> None:
    """Bring the last two days and today up to date; silently keeps stored data if Octopus or BMW is unavailable."""
    env = None
    try:
        env = _octopus_details(store)
    except ValueError:
        pass
    if env:
        live_start, live, _ = _live_rows(env)
        _attach_ev(env, live, live_start)
        _store_rows(store, live)
    try:
        sync_bmw(store)
    except (requests.RequestException, ValueError):
        pass


def fetch_consumption(store: Store, days: int) -> dict:
    start, end = _window(days)
    env = _octopus_details(store)
    if not env:
        raise ValueError("Connect your Octopus account first")
    serials = octopus.fetch_meter_serials(env["api_key"], env["account"], env["mpan"])
    rows = [r for s in serials for r in octopus.fetch_consumption(
        env["api_key"], env["mpan"], s, start.isoformat(), end.isoformat())]

    # The REST API lags behind the meter, so the last two days and today come from live telemetry
    live_start, live, live_error = _live_rows(env)
    if live:
        rows = [r for r in rows if pd.Timestamp(r["interval_start"]) < live_start] + live
    _attach_ev(env, rows, start)
    return {"fetched_days": _store_rows(store, rows), "live_slots": len(live), "live_error": live_error,
            "ev_slots": sum(1 for r in rows if r.get("ev", 0) > 0), **data_status(store)}


def _store_rows(store: Store, rows: list[dict]) -> int:
    """Group half-hourly rows into one document per local day and save them; returns the number of days."""
    if not rows:
        return 0
    cons = pd.DataFrame(rows)
    cons["start"] = pd.to_datetime(cons["interval_start"], utc=True)
    cons = cons.drop_duplicates("start", keep="first").sort_values("start")
    local = cons["start"].dt.tz_convert(LOCAL_TZ)
    cons["date"] = local.dt.strftime("%Y-%m-%d")
    mins = local.dt.hour * 60 + local.dt.minute
    cons["offpeak"] = (mins >= 23 * 60 + 30) | (mins < 5 * 60 + 30)
    has_ev = "ev" in cons.columns
    if has_ev:
        cons["ev"] = cons["ev"].fillna(0.0)
    docs = [{
        "id": d, "date": d,
        "total_kwh": round(g["consumption"].sum(), 3),
        "offpeak_kwh": round(g.loc[g["offpeak"], "consumption"].sum(), 3),
        "slots": [{"start": s.isoformat(), "kwh": k, **({"ev": e} if has_ev else {})}
                  for s, k, e in zip(g["start"], g["consumption"], g["ev"] if has_ev else g["consumption"])],
    } for d, g in cons.groupby("date")]
    store.upsert("consumption", docs)
    return len(docs)


def ensure_solar(store: Store, start: pd.Timestamp, end: pd.Timestamp, tilt: float) -> list[dict]:
    s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    docs = [d for d in store.by_date("solar", s, e) if d.get("tilt") == tilt and "faces" in d]
    # Open-Meteo archive lags a few days behind real time
    if len(docs) >= (end - start).days - 7:
        return docs
    gti = weather.fetch_faces(start.date(), (end - pd.Timedelta(days=1)).date(), tilt)
    day = (gti.index - pd.Timedelta(minutes=30)).tz_convert(LOCAL_TZ).strftime("%Y-%m-%d")
    docs = [{
        "id": d, "date": d, "location": "York", "lat": weather.YORK[0], "lon": weather.YORK[1], "tilt": tilt,
        "faces": list(weather.FACE_AZIMUTH),
        "irradiation_kwh_m2": {f: round(g[f].sum() / 1000, 3) for f in weather.FACE_AZIMUTH},
        "hourly": [{"end": t.isoformat(), **row} for t, row in g.to_dict(orient="index").items()],
    } for d, g in gti.groupby(day)]
    store.upsert("solar", docs)
    return docs


def _parse_quotes(body: dict) -> list[Inputs]:
    quotes = body.get("quotes") or [{}]
    if not isinstance(quotes, list) or not 1 <= len(quotes) <= MAX_QUOTES or not all(isinstance(q, dict) for q in quotes):
        raise ValueError(f"Provide between 1 and {MAX_QUOTES} quotes")
    return [Inputs.parse({**body, **{k: v for k, v in q.items() if k in QUOTE_FIELDS}}) for q in quotes]


def run(store: Store, body: dict) -> dict:
    """body holds the shared settings plus an optional `quotes` list overriding QUOTE_FIELDS per quote."""
    inputs = _parse_quotes(body)
    base = inputs[0]

    start, end = _window(base.days)
    tariff = Tariff(peak_p=base.peak_p, offpeak_p=base.offpeak_p, export_p=base.export_p)
    cons = store.by_date("consumption", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if not cons:
        raise ValueError("No usage data stored for this period - fetch usage data first")
    solar = ensure_solar(store, start, end, base.tilt)
    faces = base.faces()
    unit = build_frame(cons, solar, System(solar_kwp=1, performance_ratio=base.performance_ratio), tariff, faces)

    measured = annualise(unit[["pv"]])["pv"]
    scale = 1.0
    if base.typical_year and measured:
        loss = round((1 - base.performance_ratio) * 100, 1)
        typical = sum(w * weather.pvgis_kwh_per_kwp(base.tilt, f, loss) for f, w in faces.items() if w)
        scale = typical / measured
        unit["pv"] *= scale

    run_at = datetime.now(timezone.utc).isoformat()
    return {
        "data_from": unit.index.min().tz_convert(LOCAL_TZ).strftime("%Y-%m-%d"),
        "data_to": unit.index.max().tz_convert(LOCAL_TZ).strftime("%Y-%m-%d"),
        "yield_kwh_per_kwp": round(measured * scale, 0),
        "solar_scale": round(scale, 3),
        "quotes": [_run_quote(store, unit, inp, tariff, run_at) for inp in inputs],
    }


def _run_quote(store: Store, unit: pd.DataFrame, inp: Inputs, tariff: Tariff, run_at: str) -> dict:
    system = System(solar_kwp=inp.solar_kwp, battery_kwh=inp.battery_kwh, battery_kw=inp.battery_kw,
                    round_trip_eff=inp.round_trip_eff, export_limit_kw=inp.export_limit_kw,
                    performance_ratio=inp.performance_ratio, cost_gbp=inp.cost_gbp)
    df = unit.copy()
    df["pv"] *= inp.solar_kwp

    strategies = STRATEGIES
    install = pd.Timestamp(inp.install_date) if inp.install_date else pd.Timestamp.now().normalize()
    inflations = sorted({0.0, inp.price_rise_pct / 100})
    safe_name = "".join(c for c in inp.name if c not in "/\\?#")

    scenarios = []
    for strat in strategies:
        res = simulate(df, system, tariff, strat)
        res["payback"] = []
        for infl in inflations:
            be, table = payback(res["annual"]["saving_gbp"], system.cost_gbp, inflation=infl,
                                degradation=inp.degradation_pct / 100)
            res["payback"].append({
                "price_rise_pct": round(infl * 100, 2), "breakeven_years": be,
                "profit_from": (install + relativedelta(months=round(be * 12))).strftime("%b %Y") if be else None,
                "table": table,
            })
        store.upsert("simulations", [{
            "id": f"{safe_name}|{strat.name}|{run_at}", "run_at": run_at, "quote": inp.name,
            "inputs": asdict(inp), **res,
        }])
        scenarios.append(res)

    return {"name": inp.name, **{k: getattr(inp, k) for k in QUOTE_FIELDS}, "scenarios": scenarios}


def _three_day_usage(store: Store, days: list, slots: pd.DatetimeIndex) -> tuple[pd.Series, list[str], list[float], pd.Series]:
    """Actual stored usage for each day where available, otherwise the average for the same weekday over the last 8 weeks."""
    first = days[0]
    docs = store.by_date("consumption", (first - timedelta(days=TYPICAL_DAYS)).isoformat(),
                         (days[-1] + timedelta(days=1)).isoformat())
    history = [d for d in docs if d["date"] < first.isoformat()]
    if not history:
        raise ValueError("No usage data stored - fetch usage data first")
    recent = [d for d in docs if d["date"] >= first.isoformat()]
    actual = {pd.Timestamp(s["start"]): s["kwh"] for d in recent for s in d["slots"]}
    actual_ev = {pd.Timestamp(s["start"]): s.get("ev", 0.0) for d in recent for s in d["slots"]}
    day_slots = {d["date"]: len(d["slots"]) for d in recent}

    past = pd.Series({pd.Timestamp(s["start"]): s["kwh"] for d in history for s in d["slots"]})
    past_local = past.index.tz_convert(LOCAL_TZ)
    overall = past.groupby(past_local.strftime("%H:%M")).mean()
    by_weekday = past.groupby([past_local.weekday, past_local.strftime("%H:%M")]).mean()
    weekday_days = {w: len({d["date"] for d in history if date.fromisoformat(d["date"]).weekday() == w}) for w in range(7)}

    def typical_for(lt):
        hm = lt.strftime("%H:%M")
        # Same-weekday average needs at least 3 samples, otherwise use every recent day
        if weekday_days[lt.weekday()] >= 3:
            return by_weekday.get((lt.weekday(), hm), overall.get(hm))
        return overall.get(hm)

    local = slots.tz_convert(LOCAL_TZ)
    values = [actual[ts] if ts in actual else typical_for(lt) for ts, lt in zip(slots, local)]

    def source(day: date) -> str:
        n = day_slots.get(day.isoformat(), 0)
        return "actual" if n >= 46 else "partial" if n else "estimated"
    actual_kwh = [sum(v for ts, lt, v in zip(slots, local, values) if ts in actual and lt.date() == d) for d in days]
    return pd.Series(values, index=slots), [source(d) for d in days], actual_kwh, pd.Series([actual_ev.get(ts, 0.0) for ts in slots], index=slots)


def forecast(store: Store, body: dict) -> dict:
    """The last HISTORY_DAYS days: solar against usage, run through each quote's battery."""
    inputs = _parse_quotes(body)
    base = inputs[0]
    refresh_recent(store)
    today = pd.Timestamp.now(tz=LOCAL_TZ).date()
    days = [today + timedelta(days=i) for i in range(1 - HISTORY_DAYS, 1)]
    start = pd.Timestamp(days[0]).tz_localize(LOCAL_TZ)
    end = pd.Timestamp(days[-1] + timedelta(days=1)).tz_localize(LOCAL_TZ)
    slots = pd.date_range(start, end, freq="30min", inclusive="left").tz_convert("UTC")

    usage, sources, actual_kwh, ev_usage = _three_day_usage(store, days, slots)
    faces = base.faces()
    irradiance = weather.fetch_forecast_faces(base.tilt, [f for f, w in faces.items() if w], past_days=HISTORY_DAYS - 1)
    solar_docs = [{"hourly": [{"end": t.isoformat(), **row} for t, row in irradiance.to_dict(orient="index").items()]}]

    # Days BMW has synced use its recorded charging; other days use Octopus's EV readings alone
    bmw_doc = store.get("settings", BMW_DOC)
    covered_from = (bmw_doc or {}).get("covered_from")
    bmw_kwh = {pd.Timestamp(s["start"]): s["kwh"] for d in store.by_date("settings", days[0].isoformat(), (days[-1] + timedelta(days=1)).isoformat())
               if d.get("kind") == "bmw" for s in d["slots"]} if covered_from else {}
    bmw_days = [bool(covered_from) and d.isoformat() >= covered_from for d in days]
    slot_dates = slots.tz_convert(LOCAL_TZ).strftime("%Y-%m-%d")
    bmw_covered = pd.Series([bool(covered_from) and dt >= covered_from for dt in slot_dates], index=slots)
    bmw_slot_kwh = pd.Series([bmw_kwh.get(t, 0.0) for t in slots], index=slots)
    # The larger of the two is the car's load, because Octopus also sees any other EV
    ev_load = pd.concat([ev_usage, bmw_slot_kwh.where(bmw_covered, 0.0)], axis=1).max(axis=1)
    cons_docs = [{"slots": [{"start": t.isoformat(), "kwh": k, "ev": ev_load[t]} for t, k in usage.items()]}]

    tariff = Tariff(peak_p=base.peak_p, offpeak_p=base.offpeak_p, export_p=base.export_p)
    unit = build_frame(cons_docs, solar_docs, System(solar_kwp=1, performance_ratio=base.performance_ratio), tariff, faces)
    if unit.empty:
        raise ValueError("No solar forecast is available right now - try again later")

    quotes = []
    for inp in inputs:
        system = System(solar_kwp=inp.solar_kwp, battery_kwh=inp.battery_kwh, battery_kw=inp.battery_kw,
                        round_trip_eff=inp.round_trip_eff, export_limit_kw=inp.export_limit_kw,
                        performance_ratio=inp.performance_ratio, cost_gbp=inp.cost_gbp)
        df = unit.copy()
        df["pv"] *= inp.solar_kwp
        scenarios = []
        for strat in STRATEGIES:
            r = dispatch(df, system, tariff, strat)
            # The car only charges from the grid: BMW's recorded charging, plus other EV charging Octopus saw. On days BMW
            # hasn't synced, all usage inside the off-peak window is shown as "other" charging, as before
            covered = bmw_covered.reindex(r.index)
            house_grid = r["import"] - r["grid_charge"]
            bmw_part = bmw_slot_kwh.reindex(r.index).where(covered, 0.0).clip(upper=r["ev_grid"])
            window = ~covered & r["offpeak"]
            r["bmw_direct"] = 0.0
            r["bmw_discharge"] = 0.0
            r["bmw_grid_house"] = bmw_part
            r["oev_direct"] = r["direct"].where(window, 0.0)
            r["oev_discharge"] = r["discharge"].where(window, 0.0)
            r["oev_grid_house"] = (r["ev_grid"] - bmw_part).where(~window, house_grid)
            local = r.index.tz_convert(LOCAL_TZ)
            totals = r[list(FORECAST_TOTALS)].groupby(local.strftime("%Y-%m-%d")).sum().round(2)
            flows = r[list(FORECAST_FLOWS)] / SLOT_H
            flows["import"] -= flows["grid_charge"]
            scenarios.append({
                "scenario": strat.name,
                "history": [{"date": d.isoformat(), **totals.loc[d.isoformat()].to_dict()}
                            for d in days if d.isoformat() in totals.index],
                "days": [{"date": d.isoformat(), **totals.loc[d.isoformat()].to_dict()}
                         for d in days[-3:] if d.isoformat() in totals.index],
                "slots": [{"t": lt.strftime("%Y-%m-%d %H:%M"), "offpeak": bool(op),
                           **{k: round(float(row[k]), 3) for k in FORECAST_FLOWS}, "soc": round(float(soc), 3)}
                          for lt, op, soc, (_, row) in zip(local, r["offpeak"], r["soc"], flows.iterrows())
                          if lt.date() >= days[-3]],
            })
        quotes.append({"name": inp.name, "battery_kwh": inp.battery_kwh, "scenarios": scenarios})

    names = {today: "Today", today - timedelta(days=1): "Yesterday", today - timedelta(days=2): "Two days ago"}
    history = [{"date": d.isoformat(), "label": names.get(d, f"{d:%a} {d.day} {d:%b}"), "usage": src,
                "actual_kwh": round(act, 2), "charging": "bmw" if from_bmw_day else "estimated"}
               for d, src, act, from_bmw_day in zip(days, sources, actual_kwh, bmw_days)]
    return {"days": history[-3:], "history": history, "quotes": quotes}
