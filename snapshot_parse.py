"""Pure parsing helpers for Polymarket weather bucket sub-markets.

No I/O. No side effects. Safe to call from tests without fixtures.
"""
import re

_MONTHS = {m: i + 1 for i, m in enumerate([
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
])}

_SLUG_RE = re.compile(
    r"^(highest|lowest)-temperature-in-([a-z0-9-]+)-on-([a-z]+)-(\d+)-(\d{4})$"
)


def parse_event_slug(slug: str) -> dict | None:
    """Parse a Polymarket daily-temp event slug.

    Returns {"kind": "highest|lowest", "city": "<slug>", "resolution_date": "YYYY-MM-DD"}
    or None if slug doesn't match the daily-temp pattern.
    """
    if not slug:
        return None
    m = _SLUG_RE.match(slug)
    if not m:
        return None
    kind, city, month, day, year = m.groups()
    mo = _MONTHS.get(month)
    if mo is None:
        return None
    return {
        "kind": kind,
        "city": city,
        "resolution_date": f"{int(year):04d}-{mo:02d}-{int(day):02d}",
    }


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def parse_bucket_bounds(group_item_title: str) -> tuple[float | None, float | None]:
    """Parse a Polymarket bucket sub-market's groupItemTitle string.

    Returns (lo_F, hi_F) where each may be None for open-ended tails.
    Handles Fahrenheit (°F) and Celsius (°C) with auto-conversion to °F.

    Examples:
        "64-65°F"        -> (64.0, 65.0)
        "55°F or below"  -> (None, 55.0)
        "74°F or higher" -> (74.0, None)
        "be 22°C"        -> (71.6, 71.6)  # exact, both bounds same
        "22-23°C"        -> (71.6, 73.4)
        "22°C or higher" -> (71.6, None)
        "nonsense"       -> (None, None)
    """
    if not group_item_title:
        return (None, None)

    g = group_item_title.strip()
    is_celsius = "°C" in g or "C" in g.upper().split() or "celsius" in g.lower()
    is_fahrenheit = "°F" in g or "F" in g.upper().split() or "fahrenheit" in g.lower()
    # Default to fahrenheit if no unit indicator (US convention)
    use_c = is_celsius and not is_fahrenheit

    g_lower = g.lower()

    # open-bottom tail: "X or below"
    if "or below" in g_lower or "or under" in g_lower:
        nums = re.findall(r"-?\d+(?:\.\d+)?", g)
        if not nums:
            return (None, None)
        v = float(nums[0])
        if use_c:
            v = c_to_f(v)
        return (None, v)

    # open-top tail: "X or higher" / "X or above"
    if "or higher" in g_lower or "or above" in g_lower or "or more" in g_lower:
        nums = re.findall(r"-?\d+(?:\.\d+)?", g)
        if not nums:
            return (None, None)
        v = float(nums[0])
        if use_c:
            v = c_to_f(v)
        return (v, None)

    # closed range: "X-Y" (single dash between numbers, ignoring negatives)
    range_m = re.search(r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)", g)
    if range_m:
        lo = float(range_m.group(1))
        hi = float(range_m.group(2))
        if use_c:
            lo = c_to_f(lo)
            hi = c_to_f(hi)
        return (lo, hi)

    # exact temperature: single number with optional "be" prefix
    nums = re.findall(r"-?\d+(?:\.\d+)?", g)
    if nums:
        v = float(nums[0])
        if use_c:
            v = c_to_f(v)
        return (v, v)

    return (None, None)


def classify_bucket_type(group_item_title: str) -> str:
    """Returns one of: 'tail', 'range', 'exact', or 'threshold'."""
    if not group_item_title:
        return "unknown"
    g = group_item_title.lower()
    if "or below" in g or "or above" in g or "or higher" in g or "or under" in g or "or more" in g:
        return "tail"
    if re.search(r"-?\d+(?:\.\d+)?\s*-\s*-?\d+(?:\.\d+)?", group_item_title):
        return "range"
    nums = re.findall(r"-?\d+(?:\.\d+)?", group_item_title)
    if len(nums) == 1:
        return "exact"
    return "unknown"
