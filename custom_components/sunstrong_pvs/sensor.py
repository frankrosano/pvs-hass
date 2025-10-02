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
from pypvs.models.ess import PVSESS
from pypvs.models.transfer_switch import PVSTransferSwitch

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

from .const import (
    DOMAIN, 
    OPTION_ENABLE_LIVE_DATA, 
    OPTION_ENABLE_LIVE_DATA_DEFAULT_VALUE,
    OPTION_AUTO_DISABLE_UNAVAILABLE_SENSORS,
    OPTION_AUTO_DISABLE_UNAVAILABLE_SENSORS_DEFAULT_VALUE,
)
from .coordinator import PVSConfigEntry, PVSUpdateCoordinator
from .entity import PVSBaseEntity

_LOGGER = logging.getLogger(__name__)

CURRENT_POWER_KEY = "current_power_production"
LAST_REPORTED_KEY = "last_reported"


@dataclass(frozen=True, kw_only=True)
class PVSInverterSensorEntityDescription(SensorEntityDescription):
    """Describes an PVS SunStrong Management microinverter sensor entity."""

    value_fn: Callable[[PVSInverter], datetime.datetime | float]


@dataclass(frozen=True, kw_only=True)
class PVSMeterSensorEntityDescription(SensorEntityDescription):
    """Describes a built-in PVS meter sensor entity."""

    value_fn: Callable[[PVSMeter], float | datetime.datetime | None]


@dataclass(frozen=True, kw_only=True)
class PVSGatewaySensorEntityDescription(SensorEntityDescription):
    """Describes an PVS SunStrong Management gateway sensor entity."""

    value_fn: Callable[[PVSGateway], int | str | None]


@dataclass(frozen=True, kw_only=True)
class PVSESSSensorEntityDescription(SensorEntityDescription):
    """Describes an Equinox ESS sensor entity."""

    value_fn: Callable[[PVSESS], float | int | str | None]


@dataclass(frozen=True, kw_only=True)
class PVSTransferSwitchSensorEntityDescription(SensorEntityDescription):
    """Describes a MIDC transfer switch sensor entity."""

    value_fn: Callable[[PVSTransferSwitch], float | int | str | None]


@dataclass(frozen=True, kw_only=True)
class PVSLiveDataSensorEntityDescription(SensorEntityDescription):
    """Describes a PVS live data sensor entity."""

    value_fn: Callable[[dict], float | int | str | None]
    var_name: str


def _convert_live_data_value(data: dict, var_name: str, numeric: bool = True) -> float | int | str | None:
    """Convert live data value from string to appropriate type."""
    raw_value = data.get(var_name)
    if raw_value is None:
        return None
    
    if not numeric:
        return raw_value
    
    # Handle string values that should be numeric
    if isinstance(raw_value, str):
        if raw_value.lower() in ('nan', 'null', ''):
            return None
        try:
            # Try to convert to float first
            float_val = float(raw_value)
            # Check if it's actually an integer
            if float_val.is_integer():
                return int(float_val)
            return float_val
        except (ValueError, TypeError):
            return None
    
    return raw_value


def _convert_timestamp_value(data: dict, var_name: str) -> datetime.datetime | None:
    """Convert timestamp value to datetime, handling different formats."""
    raw_value = _convert_live_data_value(data, var_name)
    if raw_value is None:
        return None
    
    try:
        timestamp = int(raw_value)
        
        # Check if this looks like a reasonable Unix timestamp
        # Unix timestamps should be between 1970 and ~2038 for 32-bit
        # Current time is around 1.7 billion seconds since epoch
        current_time = dt_util.utcnow().timestamp()
        
        # If the timestamp is way in the future, it might be milliseconds
        if timestamp > current_time + (365 * 24 * 3600):  # More than 1 year in future
            _LOGGER.debug("Timestamp %s seems too far in future, trying as milliseconds", timestamp)
            timestamp = timestamp / 1000
        
        # If still unreasonable, it might be some other format
        if timestamp < 0 or timestamp > current_time + (365 * 24 * 3600):
            _LOGGER.warning("Timestamp %s doesn't look like a valid Unix timestamp", raw_value)
            return None
            
        return dt_util.utc_from_timestamp(timestamp)
        
    except (ValueError, TypeError, OSError) as e:
        _LOGGER.debug("Failed to convert timestamp %s: %s", raw_value, e)
        return None


def _should_disable_sensor_by_default(live_data: dict, description: PVSLiveDataSensorEntityDescription) -> bool:
    """Determine if a sensor should be disabled by default based on available data."""
    if not live_data:
        return False
    
    raw_value = live_data.get(description.var_name)
    
    # Disable if the raw value is 'nan', None, empty string, or '0' for certain sensors
    if raw_value is None or raw_value == '':
        return True
    
    if isinstance(raw_value, str):
        if raw_value.lower() in ('nan', 'null'):
            return True
        
        # For battery-related sensors, also disable if value is '0' (no battery)
        if ('battery' in description.key or 
            'ess' in description.var_name or 
            'soc' in description.var_name or
            'mid' in description.key.lower() or  # MID state sensors
            'midstate' in description.var_name):  # MID state variable
            if raw_value == '0' or raw_value == '0.0':
                return True
    
    return False


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
        key="lifetime_production",
        translation_key="lifetime_production",
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
        translation_key="temperature",
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

ESS_SENSORS = (
    PVSESSSensorEntityDescription(
        key="power_3ph_kw",
        translation_key="power_3ph_kw",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        value_fn=attrgetter("power_3ph_kw"),
    ),
    PVSESSSensorEntityDescription(
        key="neg_lte_kwh",
        translation_key="neg_lte_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=attrgetter("neg_lte_kwh"),
    ),
    PVSESSSensorEntityDescription(
        key="pos_lte_kwh",
        translation_key="pos_lte_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=attrgetter("pos_lte_kwh"),
    ),
    PVSESSSensorEntityDescription(
        key="v1n_v",
        translation_key="v1n_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v1n_v"),
    ),
    PVSESSSensorEntityDescription(
        key="v2n_v",
        translation_key="v2n_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v2n_v"),
    ),
    PVSESSSensorEntityDescription(
        key="op_mode",
        translation_key="op_mode",
        native_unit_of_measurement=None,
        value_fn=attrgetter("op_mode"),
    ),
    PVSESSSensorEntityDescription(
        key="soc_val",
        translation_key="soc_val",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=attrgetter("soc_val"),
    ),
    PVSESSSensorEntityDescription(
        key="customer_soc_val",
        translation_key="customer_soc_val",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=attrgetter("customer_soc_val"),
    ),
    PVSESSSensorEntityDescription(
        key="soh_val",
        translation_key="soh_val",
        native_unit_of_measurement=None,
        value_fn=attrgetter("soh_val"),
    ),
    PVSESSSensorEntityDescription(
        key="t_invtr_degc",
        translation_key="t_invtr_degc",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.TEMPERATURE,
        value_fn=attrgetter("t_invtr_degc"),
    ),
    PVSESSSensorEntityDescription(
        key="v_batt_v",
        translation_key="v_batt_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v_batt_v"),
    ),
    PVSESSSensorEntityDescription(
        key="chrg_limit_pmax_kw",
        translation_key="chrg_limit_pmax_kw",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        value_fn=attrgetter("chrg_limit_pmax_kw"),
    ),
    PVSESSSensorEntityDescription(
        key="dischrg_lim_pmax_kw",
        translation_key="dischrg_lim_pmax_kw",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        value_fn=attrgetter("dischrg_lim_pmax_kw"),
    ),
    PVSESSSensorEntityDescription(
        key="max_t_batt_cell_degc",
        translation_key="max_t_batt_cell_degc",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.TEMPERATURE,
        value_fn=attrgetter("max_t_batt_cell_degc"),
    ),
    PVSESSSensorEntityDescription(
        key="min_t_batt_cell_degc",
        translation_key="min_t_batt_cell_degc",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.TEMPERATURE,
        value_fn=attrgetter("min_t_batt_cell_degc"),
    ),
    PVSESSSensorEntityDescription(
        key="max_v_batt_cell_v",
        translation_key="max_v_batt_cell_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("max_v_batt_cell_v"),
    ),
    PVSESSSensorEntityDescription(
        key="min_v_batt_cell_v",
        translation_key="min_v_batt_cell_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("min_v_batt_cell_v"),
    ),
)

TRANSFER_SWITCH_SENSORS = (
    PVSTransferSwitchSensorEntityDescription(
        key="mid_state",
        translation_key="mid_state",
        native_unit_of_measurement=None,
        value_fn=attrgetter("mid_state"),
    ),
    PVSTransferSwitchSensorEntityDescription(
        key="pvd1_state",
        translation_key="pvd1_state",
        native_unit_of_measurement=None,
        value_fn=attrgetter("pvd1_state"),
    ),
    PVSTransferSwitchSensorEntityDescription(
        key="temperature_c",
        translation_key="temperature_c",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.TEMPERATURE,
        value_fn=attrgetter("temperature_c"),
    ),
    PVSTransferSwitchSensorEntityDescription(
        key="v1n_grid_v",
        translation_key="v1n_grid_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v1n_grid_v"),
    ),
    PVSTransferSwitchSensorEntityDescription(
        key="v1n_v",
        translation_key="v1n_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v1n_v"),
    ),
    PVSTransferSwitchSensorEntityDescription(
        key="v2n_grid_v",
        translation_key="v2n_grid_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v2n_grid_v"),
    ),
    PVSTransferSwitchSensorEntityDescription(
        key="v2n_v",
        translation_key="v2n_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v2n_v"),
    ),
    PVSTransferSwitchSensorEntityDescription(
        key="v_supply_v",
        translation_key="v_supply_v",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLTAGE,
        value_fn=attrgetter("v_supply_v"),
    ),
)

LIVE_DATA_SENSORS = (
    PVSLiveDataSensorEntityDescription(
        key="live_production_power",
        translation_key="live_production_power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        suggested_display_precision=3,
        var_name="/sys/livedata/pv_p",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/pv_p"),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_production_energy",
        translation_key="live_production_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=3,
        var_name="/sys/livedata/pv_en",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/pv_en"),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_net_consumption_power",
        translation_key="live_net_consumption_power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        suggested_display_precision=3,
        var_name="/sys/livedata/net_p",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/net_p"),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_net_consumption_energy",
        translation_key="live_net_consumption_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=3,
        var_name="/sys/livedata/net_en",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/net_en"),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_site_load_power",
        translation_key="live_site_load_power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        suggested_display_precision=3,
        var_name="/sys/livedata/site_load_p",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/site_load_p"),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_site_load_energy",
        translation_key="live_site_load_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=3,
        var_name="/sys/livedata/site_load_en",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/site_load_en"),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_battery_energy",
        translation_key="live_battery_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=3,
        var_name="/sys/livedata/ess_en",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/ess_en"),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_battery_power",
        translation_key="live_battery_power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.POWER,
        suggested_display_precision=3,
        var_name="/sys/livedata/ess_p",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/ess_p"),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_battery_soc",
        translation_key="live_battery_soc",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.BATTERY,
        suggested_display_precision=1,
        var_name="/sys/livedata/soc",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/soc"),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_backup_time_remaining",
        translation_key="live_backup_time_remaining",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.DURATION,
        var_name="/sys/livedata/backupTimeRemaining",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/backupTimeRemaining"),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_mid_state",
        translation_key="live_mid_state",
        var_name="/sys/livedata/midstate",
        value_fn=lambda data: _convert_live_data_value(data, "/sys/livedata/midstate", numeric=False),
    ),
    PVSLiveDataSensorEntityDescription(
        key="live_data_timestamp",
        translation_key="live_data_timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        var_name="/sys/livedata/time",
        value_fn=lambda data: _convert_timestamp_value(data, "/sys/livedata/time"),
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

    if pvs_data.ess:
        entities.extend(
            PVSESSEntity(coordinator, description, ess)
            for description in ESS_SENSORS
            for ess in pvs_data.ess.values()
        )

    if pvs_data.transfer_switches:
        entities.extend(
            PVSTransferSwitchEntity(coordinator, description, transfer_switch)
            for description in TRANSFER_SWITCH_SENSORS
            for transfer_switch in pvs_data.transfer_switches.values()
        )

    # Add live data sensors if enabled
    live_data_enabled = config_entry.options.get(OPTION_ENABLE_LIVE_DATA, OPTION_ENABLE_LIVE_DATA_DEFAULT_VALUE)
    auto_disable_unavailable = config_entry.options.get(
        OPTION_AUTO_DISABLE_UNAVAILABLE_SENSORS, 
        OPTION_AUTO_DISABLE_UNAVAILABLE_SENSORS_DEFAULT_VALUE
    )
    _LOGGER.debug("Live data enabled: %s, auto-disable unavailable: %s", live_data_enabled, auto_disable_unavailable)
    
    if live_data_enabled:
        live_data_entities = []
        live_data = getattr(coordinator.pvs, 'live_data', None)
        
        for description in LIVE_DATA_SENSORS:
            entity = PVSLiveDataEntity(coordinator, description)
            
            # Check if this sensor should be disabled by default
            if auto_disable_unavailable and live_data and _should_disable_sensor_by_default(live_data, description):
                entity._attr_entity_registry_enabled_default = False
                _LOGGER.debug("Disabling sensor %s by default (no valid data)", description.key)
            
            live_data_entities.append(entity)
        
        _LOGGER.debug("Adding %d live data entities", len(live_data_entities))
        entities.extend(live_data_entities)

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
        self._attr_unique_id = f"{self.pvs_serial_num}_gateway_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self.pvs_serial_num}_gateway")},
            connections={(CONNECTION_NETWORK_MAC, gateway.mac)},
            manufacturer="SunStrong Management",
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
            manufacturer="SunStrong Management",
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
            manufacturer="SunStrong Management",
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

class PVSESSEntity(PVSSensorBaseEntity):
    """PVS ESS entity."""

    entity_description: PVSESSSensorEntityDescription

    def __init__(
        self,
        coordinator: PVSUpdateCoordinator,
        description: PVSESSSensorEntityDescription,
        ess: PVSESS,
    ) -> None:
        """Initialize a PVS ESS entity."""
        super().__init__(coordinator, description)
        self._serial_number = ess.serial_number
        key = description.key
        self._attr_unique_id = f"{self._serial_number}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial_number)},
            serial_number=self._serial_number,
            sw_version="UNKNOWN",
            hw_version=ess.model,
            name=f"ESS {self._serial_number}",
            manufacturer="SunStrong Management",
            model="ESS",
            via_device=(DOMAIN, self.pvs_serial_num),
        )

    @property
    def native_value(self) -> float | int | str | None:
        """Return the state of the sensor."""
        ess = self.data.ess
        assert ess is not None
        if self._serial_number not in ess:
            _LOGGER.debug(
                "ESS %s not in returned ESS array (size: %s)",
                self._serial_number,
                len(ess),
            )
            return None
        return self.entity_description.value_fn(ess[self._serial_number])

class PVSTransferSwitchEntity(PVSSensorBaseEntity):
    """PVS transfer switch entity."""

    entity_description: PVSTransferSwitchSensorEntityDescription

    def __init__(
        self,
        coordinator: PVSUpdateCoordinator,
        description: PVSTransferSwitchSensorEntityDescription,
        transfer_switch: PVSTransferSwitch,
    ) -> None:
        """Initialize a PVS transfer switch entity."""
        super().__init__(coordinator, description)
        self._serial_number = transfer_switch.serial_number
        key = description.key
        self._attr_unique_id = f"{self._serial_number}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial_number)},
            serial_number=self._serial_number,
            sw_version="UNKNOWN",
            hw_version=transfer_switch.model,
            name=f"Transfer Switch {self._serial_number}",
            manufacturer="SunStrong Management",
            model="Transfer Switch",
            via_device=(DOMAIN, self.pvs_serial_num),
        )

    @property
    def native_value(self) -> float | int | str | None:
        """Return the state of the sensor."""
        transfer_switches = self.data.transfer_switches
        assert transfer_switches is not None
        if self._serial_number not in transfer_switches:
            _LOGGER.debug(
                "Transfer switch %s not in returned transfer switch array (size: %s)",
                self._serial_number,
                len(transfer_switches),
            )
            return None
        return self.entity_description.value_fn(transfer_switches[self._serial_number])


class PVSLiveDataEntity(PVSSensorBaseEntity):
    """PVS live data entity."""

    entity_description: PVSLiveDataSensorEntityDescription

    def __init__(
        self,
        coordinator: PVSUpdateCoordinator,
        description: PVSLiveDataSensorEntityDescription,
    ) -> None:
        """Initialize a PVS live data entity."""
        super().__init__(coordinator, description)
        self._attr_unique_id = f"{self.pvs_serial_num}_livedata_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self.pvs_serial_num}_livedata")},
            manufacturer="SunStrong Management",
            model="PVS Live Data",
            name="PVS Live Data",
            via_device=(DOMAIN, f"{self.pvs_serial_num}_gateway"),
        )

    @property
    def native_value(self) -> float | int | str | None:
        """Return the state of the sensor."""
        live_data = getattr(self.coordinator.pvs, 'live_data', None)
        if live_data is None:
            return None
        return self.entity_description.value_fn(live_data)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Show as available if live data dict exists, even if values are None
        live_data = getattr(self.coordinator.pvs, 'live_data', None)
        return super().available and live_data is not None
