from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

LOCAL_TZ = "Europe/London"
SLOT_H = 0.5


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@dataclass
class Tariff:
    peak_p: float = 28.85       # Intelligent Octopus Go, region F (inc VAT)
    offpeak_p: float = 6.90
    export_p: float = 12.0      # Outgoing Octopus
    offpeak_start: str = "23:30"
    offpeak_end: str = "05:30"


@dataclass
class System:
    solar_kwp: float = 7.76
    battery_kwh: float = 13.5   # Powerwall 3 usable capacity
    battery_kw: float = 5.0
    round_trip_eff: float = 0.89
    export_limit_kw: float = 5.0
    performance_ratio: float = 0.85
    cost_gbp: float = 13699.0


@dataclass
class Strategy:
    name: str
    grid_charge: bool = True    # top battery up at off-peak rate to cover the next day's peak deficit
    ev_surplus: bool = False    # divert surplus solar to the car in ev_months
    ev_months: tuple = field(default=(4, 5, 6, 7, 8, 9))
    ev_min_kw: float = 1.4
    ev_max_kw: float = 7.0


def build_frame(cons_docs: list[dict], solar_docs: list[dict], system: System, tariff: Tariff,
                faces: dict[str, float]) -> pd.DataFrame:
    """faces maps roof face (south/east/west) to the share of panels on it."""
    load = pd.Series({pd.Timestamp(s["start"]): s["kwh"] for d in cons_docs for s in d["slots"]}).sort_index()
    gti = pd.Series({pd.Timestamp(h["end"]): sum(h[f] * w for f, w in faces.items())
                     for d in solar_docs for h in d["hourly"]}).sort_index()
    hour_end = load.index.floor("h") + pd.Timedelta(hours=1)
    pv_kw_per_kwp = pd.Series(gti.reindex(hour_end).to_numpy(), index=load.index) / 1000 * system.performance_ratio
    df = pd.DataFrame({"load": load, "pv": pv_kw_per_kwp * system.solar_kwp * SLOT_H}).dropna()

    local = df.index.tz_convert(LOCAL_TZ)
    mins = local.hour * 60 + local.minute
    start, end = _mins(tariff.offpeak_start), _mins(tariff.offpeak_end)
    df["offpeak"] = (mins >= start) | (mins < end)
    # "session day" runs from end of off-peak to end of the following off-peak, so each day's peak precedes its night
    df["key"] = (local - pd.Timedelta(minutes=end)).date
    df["month"] = local.month
    return df


def simulate(df: pd.DataFrame, system: System, tariff: Tariff, strat: Strategy) -> dict:
    eta = system.round_trip_eff ** 0.5
    cap, lim = system.battery_kwh, system.battery_kw * SLOT_H
    exp_lim = system.export_limit_kw * SLOT_H
    ev_min, ev_max = strat.ev_min_kw * SLOT_H, strat.ev_max_kw * SLOT_H

    off, peak = df[df.offpeak], df[~df.offpeak]
    house_base = off["load"].quantile(0.2)
    ev_budget = (off["load"] - house_base).clip(lower=0).groupby(off["key"]).sum().to_dict()

    net = peak["load"] - peak["pv"]
    need = (net.clip(lower=0).groupby(peak["key"]).sum()
            - (-net).clip(lower=0).groupby(peak["key"]).sum() * system.round_trip_eff).clip(lower=0)
    keys = sorted(df["key"].unique())
    target = {k: min(cap, need.get(n, 0.0) / eta) for k, n in zip(keys, keys[1:])}

    soc = soc_solar = 0.0
    ev_div: dict = {}
    ev_credit: dict = {}
    out = []
    for t, load0, pv, offpk, key, month in df[["load", "pv", "offpeak", "key", "month"]].itertuples():
        load = load0
        if offpk and ev_credit.get(key, 0.0) > 0:
            cut = min(ev_credit[key], max(0.0, load - house_base))
            load -= cut
            ev_credit[key] -= cut

        direct = min(pv, load)
        surplus, deficit = pv - direct, load - direct

        room = lim
        c = max(0.0, min(surplus, room, (cap - soc) / eta))
        soc += c * eta
        soc_solar += c * eta
        surplus -= c
        room -= c

        ev = 0.0
        if strat.ev_surplus and not offpk and month in strat.ev_months and surplus >= ev_min:
            ev = min(surplus, ev_max, ev_budget.get(key, 0.0) - ev_div.get(key, 0.0))
            if ev >= ev_min:
                ev_div[key] = ev_div.get(key, 0.0) + ev
                ev_credit[key] = ev_credit.get(key, 0.0) + ev
                surplus -= ev
            else:
                ev = 0.0

        export = min(surplus, exp_lim)
        curtailed = surplus - export

        dis = solar_from_batt = 0.0
        if not offpk and deficit > 0 and soc > 0:
            dis = min(deficit, lim, soc * eta)
            frac = soc_solar / soc
            soc -= dis / eta
            soc_solar = max(0.0, soc_solar - dis / eta * frac)
            solar_from_batt = dis * frac
            deficit -= dis

        grid_chg = 0.0
        if offpk and strat.grid_charge:
            grid_chg = max(0.0, min(room, (target.get(key, 0.0) - soc) / eta))
            soc += grid_chg * eta

        out.append((t, load0, pv, offpk, month, direct, solar_from_batt, ev, export, curtailed,
                    deficit + grid_chg, grid_chg, c, dis, soc))

    r = pd.DataFrame(out, columns=["t", "load", "pv", "offpeak", "month", "direct", "solar_batt", "ev_solar",
                                   "export", "curtailed", "import", "grid_charge", "solar_charge",
                                   "discharge", "soc"]).set_index("t")
    rate = r["offpeak"].map({True: tariff.offpeak_p, False: tariff.peak_p}) / 100
    r["cost_before"] = r["load"] * rate
    r["cost_after"] = r["import"] * rate - r["export"] * tariff.export_p / 100

    r["peak_load"] = r["load"].where(~r["offpeak"], 0.0)
    r["offpeak_load"] = r["load"] - r["peak_load"]
    r["peak_import"] = r["import"].where(~r["offpeak"], 0.0)
    r["solar_to_peak"] = r["direct"].where(~r["offpeak"], 0.0) + r["solar_batt"]
    r["saving"] = r["cost_before"] - r["cost_after"]
    a = annualise(r)
    days = r.index.tz_convert(LOCAL_TZ).normalize().nunique()
    monthly = r.assign(ym=r.index.tz_convert(LOCAL_TZ).strftime("%Y-%m")).groupby("ym")[
        ["load", "pv", "export", "ev_solar", "import", "cost_before", "cost_after"]].sum()
    monthly["saving"] = monthly["cost_before"] - monthly["cost_after"]
    daily_pv = r["pv"].groupby(r.index.tz_convert(LOCAL_TZ).strftime("%Y-%m-%d")).sum().round(2)

    return {
        "scenario": strat.name,
        "days_simulated": int(days),
        "annual": {
            "consumption_kwh": a["load"],
            "peak_consumption_kwh": a["peak_load"],
            "offpeak_consumption_kwh": a["offpeak_load"],
            "solar_generated_kwh": a["pv"],
            "solar_to_peak_usage_kwh": a["solar_to_peak"],
            "peak_usage_shifted_to_solar_pct": 100 * a["solar_to_peak"] / a["peak_load"] if a["peak_load"] else 0.0,
            "peak_import_kwh": a["peak_import"],
            "grid_battery_charge_kwh": a["grid_charge"],
            "ev_solar_kwh": a["ev_solar"],
            "export_kwh": a["export"],
            "curtailed_kwh": a["curtailed"],
            "cost_before_gbp": a["cost_before"],
            "cost_after_gbp": a["cost_after"],
            "saving_gbp": a["saving"],
        },
        "monthly": monthly.round(2).reset_index().to_dict(orient="records"),
        "daily_generation": [{"date": d, "kwh": k} for d, k in daily_pv.items()],
        "average_day": _average_day(r),
    }


SEASONS = {"Winter": (12, 1, 2), "Spring": (3, 4, 5), "Summer": (6, 7, 8), "Autumn": (9, 10, 11)}


def _average_day(r: pd.DataFrame) -> dict:
    """Mean power (kW) per half-hour of the day for each season; soc is kWh stored."""
    slot = r.index.tz_convert(LOCAL_TZ).strftime("%H:%M")
    flows = ["load", "pv", "solar_charge", "grid_charge", "discharge", "import", "export", "ev_solar"]
    out = {}
    for season, months in SEASONS.items():
        mask = r["month"].isin(months).to_numpy()
        if not mask.any():
            continue
        g = r.loc[mask, flows + ["soc"]].groupby(slot[mask]).mean()
        g[flows] = g[flows] / SLOT_H
        g["import"] -= g["grid_charge"]
        out[season] = [{"t": t, **{k: round(v, 3) for k, v in row.items()}} for t, row in g.iterrows()]
    return out


def annualise(r: pd.DataFrame) -> dict:
    """Typical year = sum over calendar months of mean daily value x days, interpolating months with no data."""
    cols = [c for c in ("load", "peak_load", "offpeak_load", "pv", "solar_to_peak", "peak_import", "grid_charge",
                        "ev_solar", "export", "curtailed", "cost_before", "cost_after", "saving") if c in r]
    daily = r[cols].groupby(r.index.tz_convert(LOCAL_TZ).date).sum()
    daily.index = pd.to_datetime(daily.index)
    by_month = daily.groupby(daily.index.month).mean().reindex(range(1, 13))
    wrapped = pd.concat([by_month.set_axis(range(-11, 1)), by_month, by_month.set_axis(range(13, 25))])
    by_month = wrapped.interpolate().loc[1:12]
    days_in_month = pd.Series([31, 28.25, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31], index=range(1, 13))
    return by_month.mul(days_in_month, axis=0).sum().to_dict()


def payback(annual_saving: float, cost: float, inflation: float = 0.0,
            degradation: float = 0.005, years: int = 25) -> tuple[float | None, list[dict]]:
    cum, breakeven, table = -cost, None, []
    for y in range(1, years + 1):
        s = annual_saving * (1 + inflation) ** (y - 1) * (1 - degradation) ** (y - 1)
        prev, cum = cum, cum + s
        if breakeven is None and cum >= 0:
            breakeven = y - 1 + (-prev) / s
        table.append({"year": y, "saving_gbp": round(s, 2), "cumulative_gbp": round(cum, 2)})
    return breakeven, table
