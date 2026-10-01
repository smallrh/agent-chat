from typing import Any

import httpx

from app.core.config import settings


def get_city_coordinates(city: str) -> dict[str, Any]:
    """
    根据城市名称查询经纬度。

    Args:
        city: 城市名称，例如广州、深圳、北京

    Returns:
        城市名称、国家、时区、纬度、经度
    """

    params = {
        "name": city,
        "count": 1,
        "language": "zh",
        "format": "json",
    }

    try:
        response = httpx.get(
            settings.GEOCODING_URL,
            params=params,
            timeout=10,
        )

        response.raise_for_status()

        data = response.json()

    except httpx.HTTPError as e:
        raise RuntimeError(f"城市查询失败：{e}")

    results = data.get("results")

    if not results:
        raise ValueError(f"没有找到城市：{city}")

    location = results[0]

    return {
        "name": location["name"],
        "country": location.get("country"),
        "country_code": location.get("country_code"),
        "timezone": location.get("timezone"),
        "latitude": location["latitude"],
        "longitude": location["longitude"],
    }


def get_weather_data(
    latitude: float,
    longitude: float,
    forecast_days: int = 1,
) -> dict[str, Any]:
    """
    根据经纬度查询天气。

    Args:
        latitude: 纬度
        longitude: 经度
        forecast_days: 查询未来多少天
    """

    params = {
        "latitude": latitude,
        "longitude": longitude,

        # 当前天气
        "current": (
            "temperature_2m,"
            "relative_humidity_2m,"
            "apparent_temperature,"
            "weather_code,"
            "wind_speed_10m"
        ),

        # 每日天气
        "daily": (
            "weather_code,"
            "temperature_2m_max,"
            "temperature_2m_min,"
            "precipitation_probability_max,"
            "precipitation_sum"
        ),

        "timezone": "auto",
        "forecast_days": forecast_days,
    }

    try:
        response = httpx.get(
            settings.WEATHER_URL,
            params=params,
            timeout=10,
        )

        response.raise_for_status()

        return response.json()

    except httpx.HTTPError as e:
        raise RuntimeError(f"天气查询失败：{e}")


def weather(
    city: str,
    time_range: str = "current",
) -> str:
    """
    查询指定城市天气。

    Args:
        city:
            城市名称，例如广州、深圳、北京。

        time_range:
            current - 当前天气
            today   - 今天
            week    - 未来7天天气
    """

    # =========================
    # 参数校验
    # =========================

    valid_ranges = {
        "current",
        "today",
        "week",
    }

    if time_range not in valid_ranges:
        return (
            "不支持的时间范围。"
            "可选：current、today、week"
        )

    # =========================
    # 城市 → 经纬度
    # =========================

    location = get_city_coordinates(city)

    latitude = location["latitude"]
    longitude = location["longitude"]

    # =========================
    # 根据时间范围决定查询天数
    # =========================

    if time_range == "current":
        forecast_days = 1

    elif time_range == "today":
        forecast_days = 1

    else:
        forecast_days = 7

    # =========================
    # 经纬度 → 天气
    # =========================

    data = get_weather_data(
        latitude=latitude,
        longitude=longitude,
        forecast_days=forecast_days,
    )

    # =========================
    # 当前天气
    # =========================

    if time_range == "current":

        current = data["current"]

        return (
            f"城市：{location['name']}\n"
            f"国家：{location['country']}\n"
            f"时区：{location['timezone']}\n"
            f"当前时间：{current['time']}\n"
            f"温度：{current['temperature_2m']}°C\n"
            f"体感温度：{current['apparent_temperature']}°C\n"
            f"湿度：{current['relative_humidity_2m']}%\n"
            f"风速：{current['wind_speed_10m']} km/h\n"
            f"天气代码：{current['weather_code']}"
        )

    # =========================
    # 今天
    # =========================

    if time_range == "today":

        daily = data["daily"]

        return (
            f"城市：{location['name']}\n"
            f"日期：{daily['time'][0]}\n"
            f"最高温度：{daily['temperature_2m_max'][0]}°C\n"
            f"最低温度：{daily['temperature_2m_min'][0]}°C\n"
            f"降水概率："
            f"{daily['precipitation_probability_max'][0]}%\n"
            f"降水量："
            f"{daily['precipitation_sum'][0]} mm\n"
            f"天气代码：{daily['weather_code'][0]}"
        )

    # =========================
    # 本周 / 未来7天
    # =========================

    if time_range == "week":

        daily = data["daily"]

        result = [
            f"城市：{location['name']}",
            f"时区：{location['timezone']}",
            "未来7天天气：",
        ]

        for i, date in enumerate(daily["time"]):

            result.append(
                f"{date} | "
                f"最高 {daily['temperature_2m_max'][i]}°C | "
                f"最低 {daily['temperature_2m_min'][i]}°C | "
                f"降水概率 "
                f"{daily['precipitation_probability_max'][i]}% | "
                f"降水量 "
                f"{daily['precipitation_sum'][i]} mm"
            )

        return "\n".join(result)

    return "无法获取天气信息。"


# 测试连通性
# if __name__ == '__main__':
#     print(weather("广州","current"))
    """
        城市：广州
        国家：中国
        时区：Asia/Shanghai
        当前时间：2026-09-22T17:45
        温度：28.7°C
        体感温度：34.7°C
        湿度：79%
        风速：3.0 km/h
        天气代码：1
    """