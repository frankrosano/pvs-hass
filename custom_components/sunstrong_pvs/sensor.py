"""Support for PVS solar energy monitor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import datetime
import logging
from operator import attrgetter
from typing import TYPE_CHECKING

from pypvs.models.inverter import PVSInverter
from pypvs.models.meter import PVSMeter
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
    UnitOfReactivePower,
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
class PVSMeterSensorEntityDescription(SensorEntityDescription):
    """Describes a built-in PVS meter sensor entity."""

    value_fn: Callable[[PVSMeter], float | datetime.datetime | None]


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

METER_SENSORS = (
    PVSMeterSensorEntityDescription(
        key="power_3ph_kw",
        translation_key="power_3ph_kw",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        value_fn=attrgetter("power_3ph_kw"),
    ),
    PVSMeterSensorEntityDescription(
        key="voltage_3ph_v",
        translation_key="voltage_3ph_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("voltage_3ph_v"),
    ),
    PVSMeterSensorEntityDescription(
        key="current_3ph_a",
        translation_key="current_3ph_a",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.CURRENT,
        value_fn=attrgetter("current_3ph_a"),
    ),
    PVSMeterSensorEntityDescription(
        key="freq_hz",
        translation_key="freq_hz",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.FREQUENCY,
        value_fn=attrgetter("freq_hz"),
    ),
    PVSMeterSensorEntityDescription(
        key="lte_3ph_kwh",
        translation_key="lte_3ph_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=attrgetter("lte_3ph_kwh"),
    ),
    PVSMeterSensorEntityDescription(
        key="ct_scale_factor",
        translation_key="ct_scale_factor",
        native_unit_of_measurement=None,
        value_fn=attrgetter("ct_scale_factor"),
    ),
    PVSMeterSensorEntityDescription(
        key="i1_a",
        translation_key="i1_a",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.CURRENT,
        value_fn=attrgetter("i1_a"),
    ),
    PVSMeterSensorEntityDescription(
        key="i2_a",
        translation_key="i2_a",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.CURRENT,
        value_fn=attrgetter("i2_a"),
    ),
    PVSMeterSensorEntityDescription(
        key="neg_lte_kwh",
        translation_key="neg_lte_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=attrgetter("neg_lte_kwh"),
    ),
    PVSMeterSensorEntityDescription(
        key="net_lte_kwh",
        translation_key="net_lte_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=attrgetter("net_lte_kwh"),
    ),
    PVSMeterSensorEntityDescription(
        key="p1_kw",
        translation_key="p1_kw",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        value_fn=attrgetter("p1_kw"),
    ),
    PVSMeterSensorEntityDescription(
        key="p2_kw",
        translation_key="p2_kw",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        value_fn=attrgetter("p2_kw"),
    ),
    PVSMeterSensorEntityDescription(
        key="pos_lte_kwh",
        translation_key="pos_lte_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=attrgetter("pos_lte_kwh"),
    ),
    PVSMeterSensorEntityDescription(
        key="q3phsum_kvar",
        translation_key="q3phsum_kvar",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        value_fn=attrgetter("q3phsum_kvar"),
    ),
    PVSMeterSensorEntityDescription(
        key="s3phsum_kva",
        translation_key="s3phsum_kva",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        value_fn=attrgetter("s3phsum_kva"),
    ),
    PVSMeterSensorEntityDescription(
        key="tot_pf_ratio",
        translation_key="tot_pf_ratio",
        native_unit_of_measurement=None,
        value_fn=attrgetter("tot_pf_ratio"),
    ),
    PVSMeterSensorEntityDescription(
        key="v12_v",
        translation_key="v12_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v12_v"),
    ),
    PVSMeterSensorEntityDescription(
        key="v1n_v",
        translation_key="v1n_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v1n_v"),
    ),
    PVSMeterSensorEntityDescription(
        key="v2n_v",
        translation_key="v2n_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v2n_v"),
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

    if pvs_data.meters:
        entities.extend(
            PVSMeterEntity(coordinator, description, meter)
            for description in METER_SENSORS
            for meter in pvs_data.meters.values()
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

class PVSMeterEntity(PVSSensorBaseEntity):
    """PVS meter entity."""

    entity_description: PVSMeterSensorEntityDescription

    def __init__(
        self,
        coordinator: PVSUpdateCoordinator,
        description: PVSMeterSensorEntityDescription,
        meter: PVSMeter,
    ) -> None:
        """Initialize a PVS meter entity."""
        super().__init__(coordinator, description)
        self._serial_number = meter.serial_number
        key = description.key
        self._attr_unique_id = f"{self._serial_number}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial_number)},
            serial_number=self._serial_number,
            sw_version="UNKNOWN",
            hw_version=meter.model,
            name=f"Meter {self._serial_number}",
            manufacturer="Sunpower",
            model="Meter",
            via_device=(DOMAIN, self.pvs_serial_num),
        )

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        meters = self.data.meters
        assert meters is not None
        if self._serial_number not in meters:
            _LOGGER.debug(
                "Meter %s not in returned meters array (size: %s)",
                self._serial_number,
                len(meters),
            )
            return None
        return self.entity_description.value_fn(meters[self._serial_number])
