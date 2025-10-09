from __future__ import annotations
import logging
from datetime import timedelta
import asyncio
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now, as_local
from homeassistant.helpers.sun import get_astral_event_next
from homeassistant.helpers.event import async_track_point_in_utc_time

from .const import DOMAIN, PLATFORMS, DEFAULT_BASE_URL
from .api import APSClient

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    data = entry.data  # <— fix
    session = async_get_clientsession(hass)

    client = APSClient(
        app_id=data["app_id"],
        app_secret=data["app_secret"],
        sid=data["sid"],
        base_url=data.get("base_url", DEFAULT_BASE_URL),
        session=session,
    )

    # Store the last fetched data for use during night hours
    last_data = {"summary": None, "hourly": None, "date": None}
    solar_active = {"is_active": False}

    def update_solar_state():
        """Check if we're currently in solar hours (30 min after sunrise to sunset)."""
        from homeassistant.util.dt import now as dt_now
        from homeassistant.helpers.sun import get_astral_event_date
        import datetime

        current_time = dt_now()
        today = current_time.date()

        # Get sunrise and sunset for today
        sunrise = get_astral_event_date(hass, "sunrise", today)
        sunset = get_astral_event_date(hass, "sunset", today)

        if sunrise and sunset:
            # Add 30 minute buffer after sunrise
            sunrise_with_buffer = sunrise + timedelta(minutes=30)
            sunset_with_buffer = sunset + timedelta(minutes=30)
            solar_active["is_active"] = sunrise_with_buffer <= current_time <= sunset_with_buffer
            _LOGGER.debug(
                "Solar state updated: active=%s (current=%s, start=%s, end=%s)",
                solar_active["is_active"], current_time, sunrise_with_buffer, sunset_with_buffer
            )
        else:
            # Fallback if sun calculation fails
            hour = current_time.hour
            solar_active["is_active"] = 7 <= hour <= 20

    async def _async_update():
        """Fetch data from API only during solar hours."""
        try:
            update_solar_state()

            # Skip API calls during non-solar hours
            if not solar_active["is_active"]:
                _LOGGER.debug("Outside solar hours, returning cached data")
                if last_data["summary"]:
                    return last_data
                # Return minimal data if no cache available
                return {
                    "summary": {"code": 0, "data": {"lifetime": 0, "today": 0, "month": 0, "year": 0}},
                    "hourly": {"code": 0, "data": []},
                    "date": as_local(now()).date().isoformat(),
                    "solar_active": False
                }

            # 1) lifetime/today/month/year
            summary = await client.get_system_summary()
            if summary.get("code") != 0:
                raise UpdateFailed(f"APsystems summary error: {summary}")

            # 2) today's hourly series
            date_str = as_local(now()).date().isoformat()
            hourly = await client.get_system_energy_hourly(date_str)
            if hourly.get("code") != 0:
                _LOGGER.warning("APsystems hourly error: %s", hourly)
                hourly = {"code": 0, "data": []}  # degrade gracefully

            # Update last_data cache
            result = {"summary": summary, "hourly": hourly, "date": date_str, "solar_active": True}
            last_data.update(result)

            return result
        except Exception as e:
            raise UpdateFailed(str(e)) from e

    # Use a 30 minute interval to stay under API limits
    # Note summer: ~13.75 hours * 2 queries/hour * 30 = 825 queries/month
    # Switched to 60 minutes
    scan_interval = int(data.get("scan_interval", 3600))  # Default 60 minutes
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_coordinator",
        update_method=_async_update,
        update_interval=timedelta(seconds=scan_interval),
    )

    await coordinator.async_config_entry_first_refresh()

    # Set up sunrise/sunset event listeners to trigger updates
    async def handle_sun_event(event):
        """Handle sunrise/sunset events."""
        _LOGGER.info("Sun event triggered: %s", event)
        update_solar_state()
        if solar_active["is_active"]:
            # Trigger an immediate update when entering solar hours
            await coordinator.async_request_refresh()

    # Track sunrise event (with 30 minute delay)
    async def schedule_sunrise_update(now_time):
        """Schedule update 30 minutes after sunrise."""
        sunrise_time = get_astral_event_next(hass, "sunrise")
        if sunrise_time:
            delayed_sunrise = sunrise_time + timedelta(minutes=30)
            async_track_point_in_utc_time(hass, handle_sun_event, delayed_sunrise)
            _LOGGER.info("Scheduled update for 30 min after sunrise: %s", delayed_sunrise)

    # Track sunset event
    async def schedule_sunset_update(now_time):
        """Schedule update at 30 minutes after sunset."""
        sunset_time = get_astral_event_next(hass, "sunset")
        if sunset_time:
            delayed_sunset = sunset_time + timedelta(minutes=30)
            async_track_point_in_utc_time(hass, handle_sun_event, delayed_sunset)
            _LOGGER.info("Scheduled update for 30 minutes after sunset: %s", delayed_sunset)

    # Schedule the initial sun events
    await schedule_sunrise_update(now())
    await schedule_sunset_update(now())

    # Store everything needed for sensors
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "sun_handlers": {
            "sunrise": schedule_sunrise_update,
            "sunset": schedule_sunset_update
        }
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded
