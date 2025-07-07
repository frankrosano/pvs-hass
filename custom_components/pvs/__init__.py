"""The PVS6 integration."""

from __future__ import annotations

from .pypvs.pypvs.pvs import PVS

from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PLATFORMS
from .coordinator import PVSUpdateCoordinator, PVSConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: PVSConfigEntry) -> bool:
    """Set up PVS from a config entry."""

    host = entry.data.get(CONF_HOST)
    password = entry.data.get(CONF_PASSWORD)

    session = async_get_clientsession(hass)
    pvs = PVS(session=session, host=host, user="spwr_owner", password=password)

    coordinator = PVSUpdateCoordinator(hass, pvs, entry)

    await coordinator.async_config_entry_first_refresh()
    if not entry.unique_id:
        hass.config_entries.async_update_entry(entry, unique_id=pvs.serial_number)

    if entry.unique_id != pvs.serial_number:
        # If the serial number of the device does not match the unique_id
        # of the config entry, it likely means the DHCP lease has expired
        # and the device has been assigned a new IP address. We need to
        # wait for the next discovery to find the device at its new address
        # and update the config entry so we do not mix up devices.
        raise ConfigEntryNotReady(
            f"Unexpected device found at {host}; expected {entry.unique_id}, "
            f"found {pvs.serial_number}"
        )

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: PVSConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: PVSUpdateCoordinator = entry.runtime_data
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: PVSConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove an pvs config entry from a device."""
    dev_ids = {dev_id[1] for dev_id in device_entry.identifiers if dev_id[0] == DOMAIN}
    coordinator = config_entry.runtime_data
    pvs_data = coordinator.pvs.data
    pvs_serial_num = config_entry.unique_id
    if pvs_serial_num in dev_ids:
        return False
    if pvs_data:
        if pvs_data.inverters:
            for inverter in pvs_data.inverters:
                if str(inverter) in dev_ids:
                    return False
        # if pvs_data.gateway:
        #     if str(pvs_data.serial_number) in dev_ids:
        #         return False
    return True
