from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone

import pandas as pd
from dateutil.relativedelta import relativedelta

from . import octopus, weather
from .model import LOCAL_TZ, Strategy, System, Tariff, annualise, build_frame, payback, simulate
from .store import Store

ORIENTATIONS = {"east_west": "East / West", "south": "North / South (panels on south face)"}
QUOTE_FIELDS = ("name", "solar_kwp", "battery_kwh", "battery_kw", "round_trip_eff", "export_limit_kw", "cost_gbp")
MAX_QUOTES = 6


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


def fetch_consumption(store: Store, days: int) -> dict:
    start, end = _window(days)
    keys = ("base_url", "api_key", "account", "mpan", "serial")
    missing = [f"OCTOPUS_{k.upper()}" for k in keys if not os.environ.get(f"OCTOPUS_{k.upper()}")]
    if missing:
        raise ValueError(f"Octopus settings are not configured: {', '.join(missing)}")
    env = {k: os.environ[f"OCTOPUS_{k.upper()}"] for k in keys}
    # Meters get replaced; history lives on older serials. Current serial first so it wins on overlaps.
    serials = [env["serial"]] + [s for s in octopus.fetch_meter_serials(
        env["base_url"], env["api_key"], env["account"], env["mpan"]) if s != env["serial"]]
    rows = [r for s in serials for r in octopus.fetch_consumption(
        env["base_url"], env["api_key"], env["mpan"], s, start.isoformat(), end.isoformat())]
    if not rows:
        return {"fetched_days": 0, **data_status(store)}
    cons = pd.DataFrame(rows)
    cons["start"] = pd.to_datetime(cons["interval_start"], utc=True)
    cons = cons.drop_duplicates("start", keep="first").sort_values("start")
    local = cons["start"].dt.tz_convert(LOCAL_TZ)
    cons["date"] = local.dt.strftime("%Y-%m-%d")
    mins = local.dt.hour * 60 + local.dt.minute
    cons["offpeak"] = (mins >= 23 * 60 + 30) | (mins < 5 * 60 + 30)
    docs = [{
        "id": d, "date": d,
        "total_kwh": round(g["consumption"].sum(), 3),
        "offpeak_kwh": round(g.loc[g["offpeak"], "consumption"].sum(), 3),
        "slots": [{"start": s.isoformat(), "kwh": k} for s, k in zip(g["start"], g["consumption"])],
    } for d, g in cons.groupby("date")]
    store.upsert("consumption", docs)
    return {"fetched_days": len(docs), **data_status(store)}


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


def run(store: Store, body: dict) -> dict:
    """body holds the shared settings plus an optional `quotes` list overriding QUOTE_FIELDS per quote."""
    quotes = body.get("quotes") or [{}]
    if not isinstance(quotes, list) or not 1 <= len(quotes) <= MAX_QUOTES or not all(isinstance(q, dict) for q in quotes):
        raise ValueError(f"Provide between 1 and {MAX_QUOTES} quotes")
    inputs = [Inputs.parse({**body, **{k: v for k, v in q.items() if k in QUOTE_FIELDS}}) for q in quotes]
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

    strategies = [
        Strategy("Solar + battery (charged from solar only)", grid_charge=False),
        Strategy("Solar + battery + off-peak grid charging", grid_charge=True),
        Strategy("Grid charging + summer surplus to EV", grid_charge=True, ev_surplus=True),
    ]
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
