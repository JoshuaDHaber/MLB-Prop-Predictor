"""Game-time weather from Open-Meteo (free for non-commercial use, CC-BY 4.0, https://open-meteo.com)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from mlb_prop_predictor.config import TTL_WEATHER
from mlb_prop_predictor.domain import Weather
from mlb_prop_predictor.http import HttpClient, HttpError

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def get_game_weather(client: HttpClient, latitude: float, longitude: float, start: datetime) -> Weather:
    """Forecast for the hour nearest first pitch. Returns empty values if unavailable."""
    day = start.astimezone(timezone.utc).date().isoformat()
    params = {
        "latitude": round(latitude, 4),
        "longitude": round(longitude, 4),
        "hourly": "temperature_2m,wind_speed_10m,precipitation_probability",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "UTC",
        "start_date": day,
        "end_date": day,
    }
    try:
        data = client.get(FORECAST_URL, params, ttl=TTL_WEATHER).json()
    except (HttpError, ValueError) as exc:
        log.warning("weather unavailable for (%s, %s): %s", latitude, longitude, exc)
        return Weather(None, None, None)

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return Weather(None, None, None)
    target = start.astimezone(timezone.utc).replace(tzinfo=None)
    idx = min(range(len(times)), key=lambda i: abs(datetime.fromisoformat(times[i]) - target))

    def pick(key: str) -> float | None:
        values = hourly.get(key) or []
        return values[idx] if idx < len(values) else None

    return Weather(
        temp_f=pick("temperature_2m"),
        wind_mph=pick("wind_speed_10m"),
        precip_prob=pick("precipitation_probability"),
    )
