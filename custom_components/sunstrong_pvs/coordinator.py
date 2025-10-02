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

from .const import (
    INVALID_AUTH_ERRORS, 
    OPTION_UPDATE_PERIOD_S, 
    OPTION_UPDATE_PERIOD_S_DEFAULT_VALUE,
    OPTION_ENABLE_LIVE_DATA,
    OPTION_ENABLE_LIVE_DATA_DEFAULT_VALUE,
    OPTION_LIVE_DATA_UPDATE_PERIOD_S,
    OPTION_LIVE_DATA_UPDATE_PERIOD_S_DEFAULT_VALUE,
)

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
        self._live_data_tracker: CALLBACK_TYPE | None = None

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

    def _get_live_data_update_interval(self) -> timedelta:
        """Get live data update interval."""
        scan_interval_s = self.entry.options.get(
            OPTION_LIVE_DATA_UPDATE_PERIOD_S, 
            OPTION_LIVE_DATA_UPDATE_PERIOD_S_DEFAULT_VALUE
        )
        return timedelta(seconds=scan_interval_s)

    @callback
    def _async_mark_setup_complete(self) -> None:
        """Mark setup as complete and setup token refresh if needed."""
        self._setup_complete = True
        self._async_setup_live_data_tracker()

    @callback
    def _async_setup_live_data_tracker(self) -> None:
        """Set up live data tracking if enabled."""
        # Cancel existing tracker
        if self._live_data_tracker:
            self._live_data_tracker()
            self._live_data_tracker = None

        # Set up new tracker if live data is enabled
        live_data_enabled = self.entry.options.get(OPTION_ENABLE_LIVE_DATA, OPTION_ENABLE_LIVE_DATA_DEFAULT_VALUE)
        _LOGGER.debug("Setting up live data tracker, enabled: %s", live_data_enabled)
        if live_data_enabled:
            live_data_interval = self._get_live_data_update_interval()
            _LOGGER.debug("Live data update interval: %s", live_data_interval)
            self._live_data_tracker = async_track_time_interval(
                self.hass,
                self._async_update_live_data_callback,
                live_data_interval,
            )

    @callback
    def _async_update_live_data_callback(self, now: datetime.datetime) -> None:
        """Callback for live data updates."""
        self.hass.async_create_task(self._async_update_live_data_only())

    async def _async_update_live_data_only(self) -> None:
        """Update only live data without triggering full coordinator update."""
        if not self._setup_complete:
            return
            
        await self._async_update_live_data()
        # Notify listeners that live data has been updated
        self.async_update_listeners()

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
                
                # Initial live data fetch on setup
                if (self.entry.options.get(OPTION_ENABLE_LIVE_DATA, OPTION_ENABLE_LIVE_DATA_DEFAULT_VALUE) and
                    not hasattr(self.pvs, 'live_data')):
                    await self._async_update_live_data()
                
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

    async def _async_update_live_data(self) -> None:
        """Fetch live data variables from the PVS system."""
        _LOGGER.debug("Updating live data...")
        _LOGGER.debug("Available PVS methods: %s", [method for method in dir(self.pvs) if not method.startswith('_')])
        
        try:
            # List of live data variables to fetch
            live_data_vars = [
                "/sys/livedata/time",
                "/sys/livedata/pv_p",
                "/sys/livedata/pv_en",
                "/sys/livedata/net_p",
                "/sys/livedata/net_en",
                "/sys/livedata/site_load_p",
                "/sys/livedata/site_load_en",
                "/sys/livedata/ess_en",
                "/sys/livedata/ess_p",
                "/sys/livedata/soc",
                "/sys/livedata/backupTimeRemaining",
                "/sys/livedata/midstate",
            ]
            
            # Check different possible methods for accessing variables
            if hasattr(self.pvs, 'get_variables'):
                _LOGGER.debug("Using get_variables method")
                live_data = await self.pvs.get_variables(live_data_vars)
                self.pvs.live_data = live_data
                _LOGGER.debug("Live data fetched: %s", live_data)
            elif hasattr(self.pvs, 'get_variable'):
                _LOGGER.debug("Using get_variable method (fallback)")
                # Fallback to individual variable requests
                live_data = {}
                for var in live_data_vars:
                    try:
                        value = await self.pvs.get_variable(var)
                        live_data[var] = value
                    except Exception as e:
                        _LOGGER.debug("Failed to get variable %s: %s", var, e)
                        live_data[var] = None
                self.pvs.live_data = live_data
                _LOGGER.debug("Live data fetched: %s", live_data)
            elif hasattr(self.pvs, 'get_var'):
                _LOGGER.debug("Using get_var method")
                live_data = {}
                for var in live_data_vars:
                    try:
                        value = await self.pvs.get_var(var)
                        live_data[var] = value
                    except Exception as e:
                        _LOGGER.debug("Failed to get variable %s: %s", var, e)
                        live_data[var] = None
                self.pvs.live_data = live_data
                _LOGGER.debug("Live data fetched: %s", live_data)
            elif hasattr(self.pvs, 'request'):
                _LOGGER.debug("Using request method")
                # Try using a direct request method if available
                live_data = {}
                for var in live_data_vars:
                    try:
                        response = await self.pvs.request(f"GET {var}")
                        live_data[var] = response
                    except Exception as e:
                        _LOGGER.debug("Failed to get variable %s: %s", var, e)
                        live_data[var] = None
                self.pvs.live_data = live_data
                _LOGGER.debug("Live data fetched: %s", live_data)
            else:
                _LOGGER.warning("PVS object does not support variable access methods")
                _LOGGER.debug("Available methods: %s", [attr for attr in dir(self.pvs) if callable(getattr(self.pvs, attr)) and not attr.startswith('_')])
                # For now, create empty live data to make sensors available but with no values
                self.pvs.live_data = {var: None for var in live_data_vars}
                
        except Exception as e:
            _LOGGER.error("Failed to fetch live data: %s", e)
            self.pvs.live_data = None

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator."""
        if self._live_data_tracker:
            self._live_data_tracker()
            self._live_data_tracker = None
        await super().async_shutdown()

    @callback
    def async_update_options(self) -> None:
        """Update options and reconfigure live data tracking."""
        self.update_interval = self._get_update_interval()
        self._async_setup_live_data_tracker()
