"""The PVS coordinator component."""

from __future__ import annotations

import contextlib
import datetime
from datetime import timedelta
import logging
from typing import Any

from pypvs.pvs import PVS
from pypvs.exceptions import PVSError, PVSAuthenticationError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PASSWORD
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util

from .const import INVALID_AUTH_ERRORS, OPTION_UPDATE_PERIOD_S, OPTION_UPDATE_PERIOD_S_DEFAULT_VALUE

SCAN_INTERVAL = timedelta(seconds=60)

TOKEN_REFRESH_CHECK_INTERVAL = timedelta(days=1)
STALE_TOKEN_THRESHOLD = timedelta(days=30).total_seconds()
NOTIFICATION_ID = "pvs_notification"

_LOGGER = logging.getLogger(__name__)


type PVSConfigEntry = ConfigEntry[PVSUpdateCoordinator]


class PVSUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """DataUpdateCoordinator to gather data from any PVS."""

    pvs_serial_number: str
    pvs_firmware: str

    def __init__(self, hass: HomeAssistant, pvs: PVS, entry: PVSConfigEntry) -> None:
        """Initialize DataUpdateCoordinator for the PVS."""
        self.pvs = pvs
        entry_data = entry.data
        self.entry = entry
        self._setup_complete = False
        self.pvs_firmware = ""

        super().__init__(
            hass,
            _LOGGER,
            name=entry_data[CONF_NAME],
            update_interval=self._get_update_interval(),
            always_update=False,
        )

    def _get_update_interval(self) -> timedelta:
        """Get new update interval."""
        scan_interval_s = self.entry.options.get(OPTION_UPDATE_PERIOD_S, OPTION_UPDATE_PERIOD_S_DEFAULT_VALUE)
        return timedelta(seconds=scan_interval_s)

    @callback
    def _async_mark_setup_complete(self) -> None:
        """Mark setup as complete and setup token refresh if needed."""
        self._setup_complete = True

    async def _async_setup_and_authenticate(self) -> None:
        """Set up and authenticate with the PVS."""
        pvs = self.pvs

        await pvs.setup()

        assert pvs._firmware.serial is not None
        self.pvs_serial_number = pvs._firmware.serial

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch all device and sensor data from api."""
        pvs = self.pvs

        # get new update interval if changed
        self.update_interval = self._get_update_interval()

        for tries in range(2):
            try:
                if not self._setup_complete:
                    await self._async_setup_and_authenticate()
                    self._async_mark_setup_complete()
                # dump all received data in debug mode to assist troubleshooting
                pvs_data = await pvs.update()
            except INVALID_AUTH_ERRORS as err:
                if self._setup_complete and tries == 0:
                    # token likely expired or firmware changed, try to re-authenticate
                    self._setup_complete = False
                    continue
                raise ConfigEntryAuthFailed from err
            except PVSError as err:
                raise UpdateFailed(f"Error communicating with API: {err}") from err

            _LOGGER.debug("PVS data: %s", pvs_data)
            return pvs_data.raw

        raise RuntimeError("Unreachable code in _async_update_data")  # pragma: no cover
