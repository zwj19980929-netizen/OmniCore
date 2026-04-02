"""
Context hint builders for the finalize stage.

Extracted from core/graph.py (R3 refactor).
"""


def build_finalize_time_hint(current_time_context) -> str:
    if not isinstance(current_time_context, dict):
        return ""

    lines = []
    iso_datetime = str(current_time_context.get("iso_datetime", "") or "").strip()
    local_date = str(current_time_context.get("local_date", "") or "").strip()
    local_time = str(current_time_context.get("local_time", "") or "").strip()
    weekday = str(current_time_context.get("weekday", "") or "").strip()
    timezone_name = str(current_time_context.get("timezone", "") or "").strip()

    if iso_datetime:
        lines.append(f"- Current datetime: {iso_datetime}")
    if local_date:
        lines.append(f"- Current date: {local_date}")
    if local_time:
        lines.append(f"- Current local time: {local_time}")
    if weekday:
        lines.append(f"- Weekday: {weekday}")
    if timezone_name:
        lines.append(f"- Timezone: {timezone_name}")

    if not lines:
        return ""
    return "\n\nCurrent local time (authoritative):\n" + "\n".join(lines)


def build_finalize_location_hint(current_location_context) -> str:
    if not isinstance(current_location_context, dict):
        return ""

    lines = []
    location_name = str(current_location_context.get("location", "") or "").strip()
    timezone_name = str(current_location_context.get("timezone", "") or "").strip()
    source_name = str(current_location_context.get("source", "") or "").strip()

    if location_name:
        lines.append(f"- User location: {location_name}")
    if timezone_name:
        lines.append(f"- Location timezone: {timezone_name}")
    if source_name:
        lines.append(f"- Source: {source_name}")

    if not lines:
        return ""
    return "\n\nCurrent user location (authoritative):\n" + "\n".join(lines)
