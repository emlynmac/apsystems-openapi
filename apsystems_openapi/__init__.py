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

from .const import DOMAIN, PLATFORMS, DEFAULT_BASE_URL, DEFAULT_INVERTER_SCAN_INTERVAL, INVERTER_LIST_CACHE_SECONDS
from .api import APSClient

import time as _time

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

    # Inverter tracking state
    inverter_cache = {
        "list": None,                # parsed list of inverter dicts
        "list_fetched_ts": 0,        # epoch when list was last fetched
        "energy": {},                # uid -> energy data dict
        "energy_fetched_ts": 0,      # epoch when energy was last fetched
        "energy_date": None,
    }
    inverter_scan = int(data.get("inverter_scan_interval", DEFAULT_INVERTER_SCAN_INTERVAL))

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
            now_ts = _time.time()

            # ── Always discover inverters on first run (even at night) ──
            if inverter_cache["list"] is None or (
                now_ts - inverter_cache["list_fetched_ts"] > INVERTER_LIST_CACHE_SECONDS
            ):
                try:
                    inv_resp = await client.get_inverters()
                    if isinstance(inv_resp, dict) and inv_resp.get("code") == 0:
                        raw = inv_resp.get("data", [])
                        parsed = []
                        for ecu in (raw if isinstance(raw, list) else []):
                            eid = ecu.get("eid")
                            for inv in ecu.get("inverter", []):
                                parsed.append({
                                    "eid": eid,
                                    "uid": inv.get("uid"),
                                    "type": inv.get("type"),
                                })
                        inverter_cache["list"] = parsed
                        inverter_cache["list_fetched_ts"] = now_ts
                        _LOGGER.info("Discovered %d inverter(s)", len(parsed))
                    else:
                        _LOGGER.warning("Inverter list API error: %s", inv_resp)
                        if inverter_cache["list"] is None:
                            inverter_cache["list"] = []
                except Exception as exc:
                    _LOGGER.warning("Error fetching inverter list: %s", exc)
                    if inverter_cache["list"] is None:
                        inverter_cache["list"] = []

            # ── Night-time path: return cached data ──
            if not solar_active["is_active"]:
                _LOGGER.debug("Outside solar hours, returning cached data")
                if last_data["summary"]:
                    cached = dict(last_data)
                    cached["solar_active"] = False
                    cached.setdefault("inverters", inverter_cache["list"] or [])
                    cached.setdefault("inverter_energy", inverter_cache["energy"])
                    cached.setdefault("inverter_energy_date", inverter_cache["energy_date"])
                    return cached
                return {
                    "summary": {"code": 0, "data": {"lifetime": 0, "today": 0, "month": 0, "year": 0}},
                    "hourly": {"code": 0, "data": []},
                    "date": as_local(now()).date().isoformat(),
                    "solar_active": False,
                    "inverters": inverter_cache["list"] or [],
                    "inverter_energy": inverter_cache["energy"],
                    "inverter_energy_date": inverter_cache["energy_date"],
                }

            # ── Solar-hours: fetch system data (existing) ──
            summary = await client.get_system_summary()
            if summary.get("code") != 0:
                raise UpdateFailed(f"APsystems summary error: {summary}")

            date_str = as_local(now()).date().isoformat()
            hourly = await client.get_system_energy_hourly(date_str)
            if hourly.get("code") != 0:
                _LOGGER.warning("APsystems hourly error: %s", hourly)
                hourly = {"code": 0, "data": []}

            result = {"summary": summary, "hourly": hourly, "date": date_str, "solar_active": True}

            # ── Inverter energy: fetch on slower schedule ──
            if now_ts - inverter_cache["energy_fetched_ts"] >= inverter_scan:
                inv_energy = {}
                for inv in (inverter_cache["list"] or []):
                    uid = inv["uid"]
                    try:
                        resp = await client.get_inverter_energy(uid, date_str)
                        if isinstance(resp, dict) and resp.get("code") == 0:
                            inv_energy[uid] = resp.get("data", {})
                        else:
                            _LOGGER.warning("Inverter energy error for %s: %s", uid, resp)
                    except Exception as exc:
                        _LOGGER.warning("Failed to fetch energy for inverter %s: %s", uid, exc)
                inverter_cache["energy"] = inv_energy
                inverter_cache["energy_fetched_ts"] = now_ts
                inverter_cache["energy_date"] = date_str

                # Log budget estimate
                n_inv = len(inverter_cache["list"] or [])
                solar_h = 11  # rough average
                sys_calls = (solar_h * 3600 / scan_interval) * 2
                inv_calls = (solar_h * 3600 / inverter_scan) * n_inv
                est_monthly = int((sys_calls + inv_calls + 1) * 30)
                _LOGGER.info(
                    "Estimated monthly API calls: ~%d/1000 "
                    "(%d inverters, system every %ds, inverters every %ds)",
                    est_monthly, n_inv, scan_interval, inverter_scan,
                )
                if est_monthly > 900:
                    _LOGGER.warning(
                        "API budget estimate (%d/mo) is close to/exceeds the 1000/mo limit! "
                        "Consider increasing inverter_scan_interval.",
                        est_monthly,
                    )

            result["inverters"] = inverter_cache["list"] or []
            result["inverter_energy"] = inverter_cache["energy"]
            result["inverter_energy_date"] = inverter_cache["energy_date"]

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
