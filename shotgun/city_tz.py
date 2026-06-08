"""Canonical city -> IANA timezone map. Single source of truth (research +
live bot both import this). Close anchor for a city-day = resolution_date
23:59:59 in this tz."""
CITY_TZ: dict[str, str] = {
    "toronto":      "America/Toronto",
    "chicago":      "America/Chicago",
    "denver":       "America/Denver",
    "dallas":       "America/Chicago",
    "munich":       "Europe/Berlin",
    "shanghai":     "Asia/Shanghai",
    "tel aviv":     "Asia/Jerusalem",
    "istanbul":     "Europe/Istanbul",
    "hong kong":    "Asia/Hong_Kong",
    "moscow":       "Europe/Moscow",
    "paris":        "Europe/Paris",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "beijing":      "Asia/Shanghai",
    "seoul":        "Asia/Seoul",
    "london":       "Europe/London",
    "nyc":          "America/New_York",
    "taipei":       "Asia/Taipei",
    "miami":        "America/New_York",
    "ankara":       "Europe/Istanbul",
    "atlanta":      "America/New_York",
    "tokyo":        "Asia/Tokyo",
    "seattle":      "America/Los_Angeles",
    "wellington":   "Pacific/Auckland",
}
