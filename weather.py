"""Local weather fetch + speakable summary.

Primary source: Open-Meteo (open-meteo.com) — free, no API key,
backed by national weather models (ECMWF / DWD / NOAA GFS, etc.).
Optional: OpenWeatherMap Current Weather when user provides an API key.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPENWEATHER_CURRENT = "https://api.openweathermap.org/data/2.5/weather"
# Multiple IP geo endpoints — some regions block / rate-limit one provider.
IP_LOCATE_ENDPOINTS = (
    "https://ipapi.co/json/",
    "https://ipinfo.io/json",
    "http://ip-api.com/json/?fields=status,message,country,regionName,city,lat,lon",
)


class WeatherError(RuntimeError):
    pass


def _speakable_place(place: str) -> str:
    """Normalize place labels for TTS (middots / slashes → Chinese pause)."""
    p = (place or "").strip()
    for sep in (" · ", "·", " / ", " | "):
        p = p.replace(sep, "，")
    return p


@dataclass
class WeatherReport:
    place: str
    latitude: float
    longitude: float
    temperature_c: float
    humidity: int | None
    wind_kmh: float | None
    description: str
    source: str  # open-meteo | openweathermap

    def speak_text(self, *, lang: str = "zh") -> str:
        """Human sentence for TTS / subtitle."""
        place = _speakable_place(self.place)
        t = f"{self.temperature_c:.0f}"
        hum = f"，湿度 {self.humidity}%" if self.humidity is not None else ""
        wind = f"，风速约 {self.wind_kmh:.0f} 公里每小时" if self.wind_kmh is not None else ""
        if lang.startswith("en"):
            bits = [f"Weather in {place}: {self.description}, {t} degrees Celsius"]
            if self.humidity is not None:
                bits.append(f"humidity {self.humidity} percent")
            if self.wind_kmh is not None:
                bits.append(f"wind about {self.wind_kmh:.0f} kilometers per hour")
            return ". ".join(bits) + "."
        return f"{place}天气：{self.description}，气温 {t} 摄氏度{hum}{wind}。"

# WMO weather interpretation codes (Open-Meteo)
_WMO_ZH: dict[int, str] = {
    0: "晴朗",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴天",
    45: "有雾",
    48: "霜雾",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


def _http_json(url: str, *, timeout: float = 12.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "DesktopToolkit/1.9.8 (weather; +https://github.com/christiancagfr-alt/DesktopToolkit)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:240]
        raise WeatherError(f"HTTP {e.code}: {body}") from e
    except Exception as e:
        raise WeatherError(str(e)) from e


def _parse_ip_payload(data: dict) -> tuple[str, float, float] | None:
    """Normalize various IP-geo JSON shapes → (place, lat, lon)."""
    if not isinstance(data, dict):
        return None
    if str(data.get("status") or "").lower() == "fail":
        return None
    lat = data.get("latitude", data.get("lat"))
    lon = data.get("longitude", data.get("lon", data.get("lng")))
    # ipinfo uses "loc": "lat,lon"
    if (lat is None or lon is None) and data.get("loc"):
        try:
            a, b = str(data["loc"]).split(",", 1)
            lat, lon = float(a), float(b)
        except Exception:
            lat = lon = None
    if lat is None or lon is None:
        return None
    city = str(data.get("city") or "").strip()
    region = str(
        data.get("region")
        or data.get("regionName")
        or data.get("region_code")
        or ""
    ).strip()
    country = str(
        data.get("country_name") or data.get("country") or ""
    ).strip()
    parts = [p for p in (city, region, country) if p]
    place = " · ".join(parts) if parts else "当前位置"
    return place, float(lat), float(lon)


def detect_location_by_ip() -> tuple[str, float, float]:
    """Approximate location from public IP (city-level), with provider fallbacks."""
    errors: list[str] = []
    for url in IP_LOCATE_ENDPOINTS:
        try:
            data = _http_json(url, timeout=8.0)
            parsed = _parse_ip_payload(data if isinstance(data, dict) else {})
            if parsed:
                return parsed
            errors.append(f"{url}: empty coords")
        except Exception as e:
            errors.append(f"{url}: {e}")
    detail = "; ".join(errors[:2]) if errors else "unknown"
    raise WeatherError(f"无法从 IP 解析位置，请手动填写城市（{detail}）")


def _geocode_candidates(name: str) -> list[str]:
    """Build query variants — saved reports often use middot separators that geocoders reject."""
    q = (name or "").strip()
    if not q:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = (s or "").strip(" ,，")
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    add(q)
    for sep in (" · ", "·", " / ", " | ", "，", ","):
        if sep in q:
            add(q.replace(sep, ", "))
            parts = [p.strip() for p in q.split(sep) if p.strip()]
            if parts:
                add(parts[0])
                if len(parts) >= 2:
                    add(f"{parts[0]}, {parts[1]}")
            break
    return out


def geocode_place(name: str, *, language: str = "zh") -> tuple[str, float, float]:
    """Resolve city/place name via Open-Meteo geocoding."""
    candidates = _geocode_candidates(name)
    if not candidates:
        raise WeatherError("请填写地点名称")
    last_err = ""
    for q in candidates:
        url = (
            f"{OPEN_METEO_GEOCODE}?name={urllib.parse.quote(q)}"
            f"&count=1&language={urllib.parse.quote(language)}&format=json"
        )
        try:
            data = _http_json(url)
        except WeatherError as e:
            last_err = str(e)
            continue
        results = data.get("results") or []
        if not results:
            last_err = f"找不到地点：{q}"
            continue
        r0 = results[0]
        label_parts = [
            str(r0.get("name") or q),
            str(r0.get("admin1") or ""),
            str(r0.get("country") or ""),
        ]
        place = " · ".join(p for p in label_parts if p)
        return place, float(r0["latitude"]), float(r0["longitude"])
    raise WeatherError(last_err or f"找不到地点：{name}")

def fetch_open_meteo(lat: float, lon: float, place: str) -> WeatherReport:
    url = (
        f"{OPEN_METEO_FORECAST}?latitude={lat:.5f}&longitude={lon:.5f}"
        f"&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        f"&wind_speed_unit=kmh&timezone=auto"
    )
    data = _http_json(url)
    cur = data.get("current") or {}
    code = int(cur.get("weather_code") or 0)
    desc = _WMO_ZH.get(code, f"天气代码 {code}")
    hum = cur.get("relative_humidity_2m")
    wind = cur.get("wind_speed_10m")
    return WeatherReport(
        place=place,
        latitude=lat,
        longitude=lon,
        temperature_c=float(cur.get("temperature_2m") or 0.0),
        humidity=int(hum) if hum is not None else None,
        wind_kmh=float(wind) if wind is not None else None,
        description=desc,
        source="open-meteo",
    )


def fetch_openweathermap(lat: float, lon: float, place: str, api_key: str, *, lang: str = "zh_cn") -> WeatherReport:
    key = (api_key or "").strip()
    if not key:
        raise WeatherError("未填写 OpenWeatherMap API Key")
    url = (
        f"{OPENWEATHER_CURRENT}?lat={lat:.5f}&lon={lon:.5f}"
        f"&appid={urllib.parse.quote(key)}&units=metric&lang={urllib.parse.quote(lang)}"
    )
    data = _http_json(url)
    weather = (data.get("weather") or [{}])[0]
    main = data.get("main") or {}
    wind = data.get("wind") or {}
    # m/s → km/h
    speed = wind.get("speed")
    wind_kmh = float(speed) * 3.6 if speed is not None else None
    name = str(data.get("name") or place)
    return WeatherReport(
        place=name,
        latitude=lat,
        longitude=lon,
        temperature_c=float(main.get("temp") or 0.0),
        humidity=int(main["humidity"]) if main.get("humidity") is not None else None,
        wind_kmh=wind_kmh,
        description=str(weather.get("description") or "天气"),
        source="openweathermap",
    )


def _saved_coords(cfg: dict) -> tuple[str, float, float] | None:
    """Return last-known coords if present (used as auto fallback)."""
    try:
        lat = float(cfg.get("latitude"))
        lon = float(cfg.get("longitude"))
    except Exception:
        return None
    place = str(
        cfg.get("last_place") or cfg.get("location_text") or f"{lat:.2f},{lon:.2f}"
    ).strip()
    return place, lat, lon


def resolve_coords(cfg: dict) -> tuple[str, float, float]:
    """cfg: location_mode=auto|manual|coords, location_text, latitude, longitude.

    Important: auto mode must NOT geocode a previously saved display label
    (e.g. \"Boston · Massachusetts · United States\") — that used to break
    every announce after the first successful IP lookup.
    """
    mode = str(cfg.get("location_mode") or "auto").strip().lower()
    if mode == "coords":
        saved = _saved_coords(cfg)
        if not saved:
            raise WeatherError("经纬度无效")
        return saved
    if mode == "manual":
        text = str(cfg.get("location_text") or "").strip()
        if not text:
            raise WeatherError("请填写城市或地点名称")
        return geocode_place(text)
    # auto: prefer last known coords (fast/reliable), else IP; IP fail → coords again
    saved = _saved_coords(cfg)
    if saved:
        return saved
    try:
        return detect_location_by_ip()
    except WeatherError as ip_err:
        if saved:
            return saved
        raise ip_err

def fetch_weather(cfg: dict) -> WeatherReport:
    """Fetch using prefs dict under state['weather']."""
    place, lat, lon = resolve_coords(cfg)
    provider = str(cfg.get("provider") or "open-meteo").strip().lower()
    if provider in ("openweathermap", "owm"):
        return fetch_openweathermap(lat, lon, place, str(cfg.get("owm_api_key") or ""))
    return fetch_open_meteo(lat, lon, place)
