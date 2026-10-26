from __future__ import annotations

from datetime import date

import pandas as pd
import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_3/PVcalc"
YORK = (53.96, -1.08)
# Open-Meteo and PVGIS share the convention 0=south, -90=east, 90=west
FACE_AZIMUTH = {"south": 0, "east": -90, "west": 90}


def fetch_tilted_irradiance(start: date, end: date, tilt: float, azimuth: float,
                            lat: float = YORK[0], lon: float = YORK[1]) -> pd.Series:
    """Hourly plane-of-array irradiance (W/m²), indexed by UTC hour-end (Open-Meteo values are preceding-hour means)."""
    params = {"latitude": lat, "longitude": lon,
              "start_date": start.isoformat(), "end_date": end.isoformat(),
              "hourly": "global_tilted_irradiance", "tilt": tilt, "azimuth": azimuth,
              "timezone": "GMT"}
    r = requests.get(ARCHIVE_URL, params=params, timeout=60)
    r.raise_for_status()
    hourly = r.json()["hourly"]
    idx = pd.to_datetime(hourly["time"]).tz_localize("UTC")
    return pd.Series(hourly["global_tilted_irradiance"], index=idx, dtype="float")


def fetch_faces(start: date, end: date, tilt: float) -> pd.DataFrame:
    return pd.DataFrame({f: fetch_tilted_irradiance(start, end, tilt, az)
                         for f, az in FACE_AZIMUTH.items()}).dropna()


def pvgis_kwh_per_kwp(tilt: float, face: str, loss_pct: float,
                      lat: float = YORK[0], lon: float = YORK[1]) -> float:
    """Long-term average annual yield from PVGIS."""
    params = {"lat": lat, "lon": lon, "peakpower": 1, "loss": loss_pct, "angle": tilt,
              "aspect": FACE_AZIMUTH[face], "outputformat": "json"}
    r = requests.get(PVGIS_URL, params=params, timeout=60)
    r.raise_for_status()
    return r.json()["outputs"]["totals"]["fixed"]["E_y"]
