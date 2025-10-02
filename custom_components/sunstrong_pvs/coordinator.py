"""The PVS coordinator component."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
from datetime import timedelta
import json
import logging
from typing import Any

import aiohttp
from pypvs.pvs import PVS
from pypvs.exceptions import PVSError, PVSAuthenticationError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD
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
        self._websocket_session: aiohttp.ClientSession | None = None
        self._websocket_connection: aiohttp.ClientWebSocketResponse | None = None
        self._websocket_task: Any = None

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
        self._async_setup_live_data_tracker()

    @callback
    def _async_setup_live_data_tracker(self) -> None:
        """Set up live data tracking if enabled."""
        # Cancel existing tracker/websocket
        self._async_stop_live_data_tracking()

        # Set up new tracker if live data is enabled
        live_data_enabled = self.entry.options.get(OPTION_ENABLE_LIVE_DATA, OPTION_ENABLE_LIVE_DATA_DEFAULT_VALUE)
        _LOGGER.debug("Setting up live data tracker, enabled: %s", live_data_enabled)
        if live_data_enabled:
            # Start WebSocket connection for live data
            self._websocket_task = self.hass.async_create_task(self._async_start_websocket())

    @callback
    def _async_stop_live_data_tracking(self) -> None:
        """Stop live data tracking."""
        # Cancel polling tracker if it exists
        if self._live_data_tracker:
            self._live_data_tracker()
            self._live_data_tracker = None
        
        # Cancel WebSocket task if it exists
        if self._websocket_task and not self._websocket_task.done():
            self._websocket_task.cancel()
            self._websocket_task = None



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

    async def _async_start_websocket(self) -> None:
        """Start WebSocket connection for live data."""
        host = self.entry.data.get(CONF_HOST)
        websocket_url = f"ws://{host}:9002"
        
        _LOGGER.info("Starting WebSocket connection to %s", websocket_url)
        
        while True:
            try:
                if not self._websocket_session:
                    self._websocket_session = aiohttp.ClientSession()
                
                async with self._websocket_session.ws_connect(
                    websocket_url,
                    timeout=aiohttp.ClientTimeout(total=30),
                    heartbeat=30,
                ) as ws:
                    self._websocket_connection = ws
                    _LOGGER.info("WebSocket connected successfully")
                    
                    # Initialize empty live data
                    if not hasattr(self.pvs, 'live_data'):
                        self.pvs.live_data = {}
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._process_websocket_message(data)
                            except json.JSONDecodeError as e:
                                _LOGGER.debug("Failed to parse WebSocket message: %s", e)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            _LOGGER.error("WebSocket error: %s", ws.exception())
                            break
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            _LOGGER.info("WebSocket connection closed")
                            break
                            
            except Exception as e:
                _LOGGER.error("WebSocket connection failed: %s", e)
                
            # Clean up connection
            self._websocket_connection = None
            
            # Wait before reconnecting (unless we're shutting down)
            if not self._setup_complete:
                break
                
            _LOGGER.info("Reconnecting WebSocket in 10 seconds...")
            await asyncio.sleep(10)

    async def _process_websocket_message(self, data: dict) -> None:
        """Process incoming WebSocket message."""
        if data.get("notification") == "power" and "params" in data:
            params = data["params"]
            _LOGGER.debug("WebSocket received params: %s", params)
            
            # All livedata variables from the CSV (WebSocket field -> full path)
            # In WebSocket, /sys/livedata/ is truncated, so we map the short names
            all_livedata_fields = {
                "time": "/sys/livedata/time",
                "pv_p": "/sys/livedata/pv_p",
                "pv_en": "/sys/livedata/pv_en", 
                "net_p": "/sys/livedata/net_p",
                "net_en": "/sys/livedata/net_en",
                "site_load_p": "/sys/livedata/site_load_p",
                "site_load_en": "/sys/livedata/site_load_en",
                "ess_en": "/sys/livedata/ess_en",
                "ess_p": "/sys/livedata/ess_p",
                "soc": "/sys/livedata/soc",
                "backupTimeRemaining": "/sys/livedata/backupTimeRemaining",
                "midstate": "/sys/livedata/midstate",
            }
            
            # Convert WebSocket data to live data format
            live_data = {}
            for ws_field, live_data_var in all_livedata_fields.items():
                if ws_field in params:
                    live_data[live_data_var] = str(params[ws_field])
                else:
                    # Set appropriate default values for missing fields
                    if ws_field in ["ess_en", "ess_p", "soc", "backupTimeRemaining", "midstate"]:
                        live_data[live_data_var] = "nan"  # Battery/MID fields default to nan (will be auto-disabled)
                    else:
                        live_data[live_data_var] = None   # Other fields default to None
            
            # Update live data
            self.pvs.live_data = live_data
            
            # Notify listeners that live data has been updated
            self.async_update_listeners()
            
            _LOGGER.debug("WebSocket live data updated: %s", live_data)

    async def _async_update_live_data(self) -> None:
        """Legacy method - now handled by WebSocket."""
        # This method is kept for compatibility but WebSocket handles live data now
        _LOGGER.debug("Live data updates now handled by WebSocket")
        pass

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator."""
        # Stop live data tracking
        self._async_stop_live_data_tracking()
        
        # Close WebSocket connection
        if self._websocket_connection and not self._websocket_connection.closed:
            await self._websocket_connection.close()
            
        # Close WebSocket session
        if self._websocket_session and not self._websocket_session.closed:
            await self._websocket_session.close()
            
        await super().async_shutdown()

    @callback
    def async_update_options(self) -> None:
        """Update options and reconfigure live data tracking."""
        self.update_interval = self._get_update_interval()
        self._async_setup_live_data_tracker()
