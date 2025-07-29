"""Support for PVS solar energy monitor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import datetime
import logging
from operator import attrgetter
from typing import TYPE_CHECKING

from pypvs.models.inverter import PVSInverter
from pypvs.models.gateway import PVSGateway

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import PVSConfigEntry, PVSUpdateCoordinator
from .entity import PVSBaseEntity

_LOGGER = logging.getLogger(__name__)

CURRENT_POWER_KEY = "current_power_production"
LAST_REPORTED_KEY = "last_reported"


@dataclass(frozen=True, kw_only=True)
class PVSInverterSensorEntityDescription(SensorEntityDescription):
    """Describes an PVS Sunpower/Enphase microinverter sensor entity."""

    value_fn: Callable[[PVSInverter], datetime.datetime | float]

@dataclass(frozen=True, kw_only=True)
class PVSGatewaySensorEntityDescription(SensorEntityDescription):
    """Describes an PVS Sunpower/Enphase gateway sensor entity."""

    value_fn: Callable[[PVSGateway], int | str | None]


INVERTER_SENSORS = (
    PVSInverterSensorEntityDescription(
        key=CURRENT_POWER_KEY,
        translation_key=CURRENT_POWER_KEY,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        suggested_display_precision=1,
        value_fn=attrgetter("last_report_kw"),
    ),
    PVSInverterSensorEntityDescription(
        key=LAST_REPORTED_KEY,
        translation_key=LAST_REPORTED_KEY,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda inverter: dt_util.utc_from_timestamp(inverter.last_report_date),
    ),
    PVSInverterSensorEntityDescription(
        key="lifetime_consumption",
        translation_key="lifetime_consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=3,
        value_fn=attrgetter("lte_kwh"),
    ),
    PVSInverterSensorEntityDescription(
        key="production_current",
        translation_key="production_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.CURRENT,
        suggested_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=3,
        value_fn=attrgetter("last_report_current_a"),
    ),
    PVSInverterSensorEntityDescription(
        key="production_voltage",
        translation_key="production_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        suggested_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        value_fn=attrgetter("last_report_voltage_v"),
    ),
    PVSInverterSensorEntityDescription(
        key="frequency",
        translation_key="net_ct_frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.FREQUENCY,
        suggested_display_precision=1,
        value_fn=attrgetter("last_report_frequency_hz"),
    ),
    PVSInverterSensorEntityDescription(
        key="temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        value_fn=attrgetter("last_report_temperature_c"),
    ),
)

GATEWAY_SENSORS = (
    PVSGatewaySensorEntityDescription(
        key="gateway_uptime",
        translation_key="gateway_uptime",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=attrgetter("uptime_s"),
    ),
    PVSGatewaySensorEntityDescription(
        key="ram_usage",
        translation_key="ram_usage",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=attrgetter("ram_usage_percent"),
    ),
    PVSGatewaySensorEntityDescription(
        key="flash_usage",
        translation_key="flash_usage",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=attrgetter("flash_usage_percent"),
    ),
    PVSGatewaySensorEntityDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=attrgetter("cpu_usage_percent"),
    ),
)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PVSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PVS sensor platform."""
    coordinator = config_entry.runtime_data
    pvs_data = coordinator.pvs.data
    assert pvs_data is not None
    _LOGGER.debug("PVS data: %s", pvs_data)

    entities: list[Entity] = []

    if pvs_data.gateway:
        entities.extend(
            PVSGatewayEntity(coordinator, description, pvs_data.gateway)
            for description in GATEWAY_SENSORS
        )

    if pvs_data.inverters:
        entities.extend(
            PVSInverterEntity(coordinator, description, inverter)
            for description in INVERTER_SENSORS
            for inverter in pvs_data.inverters.values()
        )

    async_add_entities(entities)


class PVSSensorBaseEntity(PVSBaseEntity, SensorEntity):
    """Defines a base PVS entity."""


class PVSGatewayEntity(PVSSensorBaseEntity):
    """PVS gateway entity."""

    def __init__(
        self,
        coordinator: PVSUpdateCoordinator,
        description: SensorEntityDescription,
        gateway: PVSGateway,
    ) -> None:
        """Initialize a PVS gateway entity."""
        super().__init__(coordinator, description)
        self._attr_unique_id = f"{self.pvs_serial_num}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.pvs_serial_num)},
            connections={(CONNECTION_NETWORK_MAC, gateway.mac)},
            manufacturer="Sunpower",
            model=gateway.model,
            name="PVS Gateway",
            sw_version=gateway.software_version,
            hw_version=gateway.hardware_version,
            serial_number=self.pvs_serial_num,
        )
    @property
    def native_value(self) -> int | str | None:
        """Return the state of the sensor."""
        gateway = self.data.gateway
        assert gateway is not None
        return self.entity_description.value_fn(gateway)

class PVSInverterEntity(PVSSensorBaseEntity):
    """PVS inverter entity."""

    entity_description: PVSInverterSensorEntityDescription

    def __init__(
        self,
        coordinator: PVSUpdateCoordinator,
        description: PVSInverterSensorEntityDescription,
        inverter: PVSInverter,
    ) -> None:
        """Initialize a PVS inverter entity."""
        super().__init__(coordinator, description)
        self._serial_number = inverter.serial_number
        key = description.key
        self._attr_unique_id = f"{self._serial_number}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial_number)},
            serial_number=self._serial_number,
            sw_version="UNKNOWN",
            hw_version=inverter.model,
            name=f"MI {self._serial_number}",
            manufacturer="Sunpower/Enphase",
            model="Inverter",
            via_device=(DOMAIN, self.pvs_serial_num),
        )

    @property
    def native_value(self) -> datetime.datetime | float | None:
        """Return the state of the sensor."""
        inverters = self.data.inverters
        assert inverters is not None
        # TODO: Does the PVS also have this problem?
        # Some envoy fw versions return an empty inverter array every 4 hours when
        # no production is taking place. Prevent collection failure due to this
        # as other data seems fine. Inverters will show unknown during this cycle.
        if self._serial_number not in inverters:
            _LOGGER.debug(
                "Inverter %s not in returned inverters array (size: %s)",
                self._serial_number,
                len(inverters),
            )
            return None
        return self.entity_description.value_fn(inverters[self._serial_number])
