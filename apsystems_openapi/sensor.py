from __future__ import annotations
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import StateType
import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator = store["coordinator"]
    sid = entry.data["sid"]

    entities = [
        APSLifetimeEnergySensor(coordinator, sid),
        APSTodayEnergySensor(coordinator, sid),
    ]

    # Create per-inverter sensors from discovered inverter list
    for inv in coordinator.data.get("inverters", []):
        entities.append(APSInverterPowerSensor(coordinator, sid, inv))

    async_add_entities(entities)

class APSBaseEntity(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, sid: str, name_suffix: str):
        super().__init__(coordinator)
        self._sid = sid
        self._attr_unique_id = f"{sid}_{name_suffix}"

    @property
    def device_info(self):
        # Ensures a device tile appears in the UI
        return {
            "identifiers": {(DOMAIN, self._sid)},
            "manufacturer": "APsystems",
            "name": f"APsystems {self._sid}",
        }

class APSLifetimeEnergySensor(APSBaseEntity):
    """Monotonic lifetime kWh for Energy dashboard."""

    _attr_name = "Total Energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, sid: str):
        super().__init__(coordinator, sid, "total_energy")

    @property
    def native_value(self) -> StateType:
        summary = self.coordinator.data.get("summary", {})
        if summary and summary.get("code") == 0:
            data = summary.get("data", {})
            try:
                return float(data.get("lifetime"))
            except (TypeError, ValueError):
                return None
        return None

    @property
    def extra_state_attributes(self):
        summary = self.coordinator.data.get("summary", {}).get("data", {}) or {}
        hourly = self.coordinator.data.get("hourly", {}) or {}
        solar_active = self.coordinator.data.get("solar_active", True)

        return {
            "today_kwh": _safe_float(summary.get("today")),
            "month_kwh": _safe_float(summary.get("month")),
            "year_kwh": _safe_float(summary.get("year")),
            "hourly_kwh": hourly.get("data"),
            "hourly_date": self.coordinator.data.get("date"),
            "source": "APsystems OpenAPI",
            "solar_hours_active": solar_active,
            "status": "Solar hours" if solar_active else "Night hours (cached data)"
        }

class APSTodayEnergySensor(APSBaseEntity):
    """Non-monotonic daily energy (kWh); resets each day."""

    _attr_name = "Today Energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, sid: str):
        super().__init__(coordinator, sid, "today_energy")

    @property
    def native_value(self) -> StateType:
        hourly = self.coordinator.data.get("hourly", {})
        if hourly and hourly.get("code") == 0:
            series = hourly.get("data") or []
            try:
                total = round(sum(float(x) for x in series if x is not None), 3)
                # During night hours, preserve the last known total
                if not self.coordinator.data.get("solar_active", True) and total == 0:
                    # Try to get from summary data instead
                    summary = self.coordinator.data.get("summary", {})
                    if summary and summary.get("code") == 0:
                        data = summary.get("data", {})
                        return _safe_float(data.get("today"))
                return total
            except (TypeError, ValueError):
                return None
        return None

    @property
    def extra_state_attributes(self):
        hourly = self.coordinator.data.get("hourly", {}) or {}
        solar_active = self.coordinator.data.get("solar_active", True)

        return {
            "hourly_kwh": hourly.get("data"),
            "hourly_date": self.coordinator.data.get("date"),
            "solar_hours_active": solar_active,
            "status": "Solar hours" if solar_active else "Night hours (cached data)"
        }

def _safe_float(v):
    try:
        return float(v)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-inverter power sensor
# ---------------------------------------------------------------------------

class APSInverterPowerSensor(APSBaseEntity):
    """AC output power of a single micro-inverter (latest reading)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator, sid: str, inverter_info: dict):
        self._uid = inverter_info["uid"]
        self._inv_type = inverter_info.get("type", "Unknown")
        self._eid = inverter_info.get("eid", "")
        super().__init__(coordinator, sid, f"inverter_{self._uid}_power")
        self._attr_name = f"Inverter {self._uid} Power"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._uid)},
            "manufacturer": "APsystems",
            "name": f"Inverter {self._uid}",
            "model": self._inv_type,
            "via_device": (DOMAIN, self._sid),
        }

    # -- helpers --

    @staticmethod
    def _latest(series):
        """Return the last non-null numeric value in a list."""
        for v in reversed(series or []):
            try:
                f = float(v)
                if f == f:  # skip NaN
                    return round(f, 1)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _peak(series):
        """Return the peak numeric value in a list."""
        try:
            vals = [float(x) for x in (series or []) if x is not None]
            return round(max(vals), 1) if vals else None
        except (TypeError, ValueError):
            return None

    # -- HA properties --

    @property
    def native_value(self) -> StateType:
        energy = self.coordinator.data.get("inverter_energy", {}).get(self._uid, {})
        return self._latest(energy.get("ac_p"))

    @property
    def extra_state_attributes(self):
        energy = self.coordinator.data.get("inverter_energy", {}).get(self._uid, {})

        dc_p1 = energy.get("dc_p1", [])
        dc_p2 = energy.get("dc_p2", [])
        ac_p = energy.get("ac_p", [])
        times = energy.get("t", [])

        return {
            "inverter_uid": self._uid,
            "inverter_type": self._inv_type,
            "ecu_id": self._eid,
            "dc_channel1_power_w": self._latest(dc_p1),
            "dc_channel2_power_w": self._latest(dc_p2),
            "dc_channel1_peak_w": self._peak(dc_p1),
            "dc_channel2_peak_w": self._peak(dc_p2),
            "ac_power_peak_w": self._peak(ac_p),
            "hourly_ac_power": ac_p,
            "hourly_dc_p1": dc_p1,
            "hourly_dc_p2": dc_p2,
            "hourly_times": times,
            "data_date": self.coordinator.data.get("inverter_energy_date"),
        }
