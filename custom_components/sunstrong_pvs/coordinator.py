"""The PVS coordinator component."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
from datetime import timedelta
import json
import logging
import random
from typing import Any

import aiohttp
from pypvs.pvs import PVS
from pypvs.exceptions import PVSError, PVSAuthenticationError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, EVENT_HOMEASSISTANT_STOP, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util

from .const import (
    INVALID_AUTH_ERRORS, 
    OPTION_UPDATE_PERIOD_S, 
    OPTION_UPDATE_PERIOD_S_DEFAULT_VALUE,
    OPTION_ENABLE_LIVE_DATA,
    OPTION_ENABLE_LIVE_DATA_DEFAULT_VALUE,
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
        self._websocket_task: Any = None
        self._stop_listener: CALLBACK_TYPE | None = None
        # Track live data entity listeners by var_name for granular updates
        self._live_data_listeners: dict[str, list[CALLBACK_TYPE]] = {}
        # Cache timestamp format: None=unknown, 'seconds'=Unix seconds, 'milliseconds'=Unix milliseconds
        self._timestamp_format: str | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=entry_data[CONF_NAME],
            update_interval=self._get_update_interval(),
            always_update=False,
        )
        
        # Listen for Home Assistant stop event to ensure cleanup
        self._stop_listener = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_ha_stop_listener
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
        if live_data_enabled:
            # Schedule WebSocket startup after Home Assistant is fully started
            if self.hass.is_running:
                # HA is already running, start immediately with delay
                self._websocket_task = self.hass.async_create_task(self._async_start_websocket_delayed())
            else:
                # HA is still starting, wait for it to be ready
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, 
                    self._async_ha_started_listener
                )

    @callback
    def _async_stop_live_data_tracking(self) -> None:
        """Stop live data tracking."""
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

    async def _async_start_websocket_delayed(self) -> None:
        """Start WebSocket connection with a delay to avoid blocking startup."""
        # Wait a bit to let Home Assistant finish startup completely
        await asyncio.sleep(5)
        await self._async_start_websocket()

    async def _async_start_websocket(self) -> None:
        """Start WebSocket connection for live data with fresh session per connection."""
        host = self.entry.data.get(CONF_HOST)
        websocket_url = f"ws://{host}:9002"
        
        _LOGGER.info("Starting WebSocket connection to %s", websocket_url)
        
        reconnect_count = 0
        fast_retry_limit = 3  # Number of fast retries before exponential backoff
        fast_retry_delay = 2  # Fast retry delay in seconds
        backoff_delay = 5  # Start with 5 seconds for exponential backoff
        max_backoff = 300  # Max 5 minutes
        stale_timeout = 90  # Consider connection stale if no messages for 90 seconds
        
        while True:
            session = None
            last_message_time = None
            heartbeat_task = None
            
            try:
                # Create fresh session for each connection attempt
                session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30, connect=10),
                    connector=aiohttp.TCPConnector(
                        limit=1,  # Only need one connection
                        limit_per_host=1,
                        ttl_dns_cache=300,  # 5 minute DNS cache
                        use_dns_cache=True,
                        enable_cleanup_closed=True,
                    )
                )
                
                _LOGGER.info("Attempting WebSocket connection to %s (attempt %d)", 
                            websocket_url, reconnect_count + 1)
                
                async with session.ws_connect(
                    websocket_url,
                    heartbeat=30,
                    compress=0,  # Disable compression for better performance
                ) as ws:
                    reconnect_count = 0  # Reset on successful connection
                    backoff_delay = 5    # Reset backoff
                    _LOGGER.info("WebSocket connected successfully to %s", websocket_url)
                    
                    # Initialize live data with None values for all expected fields
                    # This prevents entity state flapping on initial connection
                    if not hasattr(self.pvs, 'live_data') or self.pvs.live_data is None:
                        self.pvs.live_data = {
                            live_data_var: None
                            for _, (live_data_var, _) in self._LIVEDATA_FIELD_MAP.items()
                        }
                        _LOGGER.debug("Initialized live_data dict with %d fields", len(self.pvs.live_data))
                    
                    # Reset timestamp format cache on new connection
                    self._timestamp_format = None
                    
                    # Track last message time for stale connection detection
                    last_message_time = asyncio.get_event_loop().time()
                    
                    # Start heartbeat monitoring task
                    async def monitor_heartbeat():
                        """Monitor for stale connections."""
                        while True:
                            await asyncio.sleep(30)  # Check every 30 seconds
                            if last_message_time:
                                elapsed = asyncio.get_event_loop().time() - last_message_time
                                if elapsed > stale_timeout:
                                    _LOGGER.warning(
                                        "WebSocket connection appears stale (no messages for %.0f seconds), closing",
                                        elapsed
                                    )
                                    await ws.close()
                                    break
                    
                    heartbeat_task = asyncio.create_task(monitor_heartbeat())
                    
                    # Message processing loop
                    async for msg in ws:
                        # Update last message time
                        last_message_time = asyncio.get_event_loop().time()
                        
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._process_websocket_message(data)
                            except json.JSONDecodeError:
                                # Log but don't break connection for bad messages
                                _LOGGER.debug("Invalid JSON in WebSocket message")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            _LOGGER.warning("WebSocket error: %s", ws.exception())
                            break
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            _LOGGER.info("WebSocket connection closed by server")
                            break
                            
            except asyncio.CancelledError:
                _LOGGER.debug("WebSocket connection cancelled")
                break
            except Exception as e:
                reconnect_count += 1
                _LOGGER.error("WebSocket connection failed (attempt %d): %s", 
                             reconnect_count, e)
            finally:
                # Cancel heartbeat monitoring task
                if heartbeat_task and not heartbeat_task.done():
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
                
                # Clear live data on disconnect to maintain clean state
                if hasattr(self.pvs, 'live_data'):
                    self.pvs.live_data = None
                
                # Always clean up session
                if session and not session.closed:
                    await session.close()
            
            # Check if we should continue reconnecting
            if not self._setup_complete or self.hass.is_stopping:
                break
            
            # Determine retry delay: fast retry or exponential backoff
            if reconnect_count <= fast_retry_limit:
                # Fast retry for transient failures
                actual_delay = fast_retry_delay
                _LOGGER.info("Fast retry in %d seconds (attempt %d/%d)...", 
                           actual_delay, reconnect_count, fast_retry_limit)
            else:
                # Exponential backoff with jitter for persistent failures
                delay = min(backoff_delay * (2 ** min(reconnect_count - fast_retry_limit - 1, 5)), max_backoff)
                jitter = random.uniform(0.8, 1.2)  # ±20% jitter
                actual_delay = delay * jitter
                _LOGGER.info("Reconnecting in %.1f seconds (exponential backoff)...", actual_delay)
                backoff_delay = min(backoff_delay * 1.5, max_backoff)  # Gradual increase
            
            try:
                await asyncio.sleep(actual_delay)
            except asyncio.CancelledError:
                _LOGGER.debug("WebSocket reconnection cancelled")
                break

    # WebSocket field mapping (short name -> full path, type)
    # Type: 'numeric' for float/int, 'string' for text, 'timestamp' for datetime
    _LIVEDATA_FIELD_MAP = {
        "time": ("/sys/livedata/time", "timestamp"),
        "pv_p": ("/sys/livedata/pv_p", "numeric"),
        "pv_en": ("/sys/livedata/pv_en", "numeric"),
        "net_p": ("/sys/livedata/net_p", "numeric"),
        "net_en": ("/sys/livedata/net_en", "numeric"),
        "site_load_p": ("/sys/livedata/site_load_p", "numeric"),
        "site_load_en": ("/sys/livedata/site_load_en", "numeric"),
        "ess_en": ("/sys/livedata/ess_en", "numeric"),
        "ess_p": ("/sys/livedata/ess_p", "numeric"),
        "soc": ("/sys/livedata/soc", "numeric"),
        "backupTimeRemaining": ("/sys/livedata/backupTimeRemaining", "numeric"),
        "midstate": ("/sys/livedata/midstate", "string"),
    }

    def _convert_websocket_value(self, raw_value: Any, value_type: str) -> float | int | str | datetime.datetime | None:
        """Convert WebSocket value to appropriate native type."""
        if raw_value is None:
            return None
        
        if value_type == "string":
            return str(raw_value)
        
        if value_type == "numeric":
            # Handle string values that should be numeric
            if isinstance(raw_value, str):
                if raw_value.lower() in ('nan', 'null', ''):
                    return None
                try:
                    float_val = float(raw_value)
                    # Return int if it's a whole number
                    if float_val.is_integer():
                        return int(float_val)
                    return float_val
                except (ValueError, TypeError):
                    return None
            # Already numeric
            return raw_value
        
        if value_type == "timestamp":
            try:
                # Convert to int first
                if isinstance(raw_value, str):
                    timestamp = int(raw_value)
                else:
                    timestamp = int(raw_value)
                
                # Use cached format if available, otherwise detect and cache
                if self._timestamp_format == "milliseconds":
                    # Known to be milliseconds, convert directly
                    timestamp = timestamp / 1000
                elif self._timestamp_format == "seconds":
                    # Known to be seconds, use as-is
                    pass
                else:
                    # Format unknown, detect and cache for future use
                    current_time = dt_util.utcnow().timestamp()
                    
                    # If timestamp is way in the future, it's likely milliseconds
                    if timestamp > current_time + (365 * 24 * 3600):
                        self._timestamp_format = "milliseconds"
                        timestamp = timestamp / 1000
                    else:
                        self._timestamp_format = "seconds"
                
                # Final validation
                current_time = dt_util.utcnow().timestamp()
                if timestamp < 0 or timestamp > current_time + (365 * 24 * 3600):
                    return None
                
                return dt_util.utc_from_timestamp(timestamp)
            except (ValueError, TypeError, OSError):
                return None
        
        return None

    @callback
    def async_add_live_data_listener(self, var_name: str, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Add a listener for a specific live data variable.
        
        Returns a callback to remove the listener.
        """
        if var_name not in self._live_data_listeners:
            self._live_data_listeners[var_name] = []
        
        self._live_data_listeners[var_name].append(update_callback)
        
        # Return a function to remove this listener
        def remove_listener() -> None:
            if var_name in self._live_data_listeners:
                self._live_data_listeners[var_name].remove(update_callback)
                if not self._live_data_listeners[var_name]:
                    del self._live_data_listeners[var_name]
        
        return remove_listener

    @callback
    def _async_notify_live_data_listeners(self, changed_vars: set[str]) -> None:
        """Notify only the listeners for variables that changed."""
        for var_name in changed_vars:
            if var_name in self._live_data_listeners:
                for update_callback in self._live_data_listeners[var_name]:
                    update_callback()

    async def _process_websocket_message(self, data: dict) -> None:
        """Process incoming WebSocket message, converting and storing values in native types."""
        if data.get("notification") != "power" or "params" not in data:
            return
        
        params = data["params"]
        
        # live_data is guaranteed to be initialized by WebSocket connection
        # but check for safety in case message arrives during shutdown
        if not hasattr(self.pvs, 'live_data') or self.pvs.live_data is None:
            return
        
        # Track which specific fields changed
        changed_vars: set[str] = set()
        
        # Update only the fields present in the message, converting to native types
        for ws_field, (live_data_var, value_type) in self._LIVEDATA_FIELD_MAP.items():
            if ws_field in params:
                # Convert to native type at write-time
                new_value = self._convert_websocket_value(params[ws_field], value_type)
                # Only update if value changed
                if self.pvs.live_data.get(live_data_var) != new_value:
                    self.pvs.live_data[live_data_var] = new_value
                    changed_vars.add(live_data_var)
        
        # Only notify listeners for fields that actually changed
        if changed_vars:
            self._async_notify_live_data_listeners(changed_vars)

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator."""
        # Stop live data tracking (cancels WebSocket task)
        self._async_stop_live_data_tracking()
        
        # Wait for WebSocket task to complete cancellation
        if self._websocket_task and not self._websocket_task.done():
            try:
                await asyncio.wait_for(self._websocket_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass  # Expected when cancelling
        
        # Remove stop listener
        if self._stop_listener:
            self._stop_listener()
            self._stop_listener = None
            
        await super().async_shutdown()

    @callback
    def _async_ha_started_listener(self, event) -> None:
        """Handle Home Assistant started event."""
        self._websocket_task = self.hass.async_create_task(self._async_start_websocket_delayed())

    @callback
    def _async_ha_stop_listener(self, event) -> None:
        """Handle Home Assistant stop event."""
        self.hass.async_create_task(self._async_cleanup_websocket())

    async def _async_cleanup_websocket(self) -> None:
        """Clean up WebSocket connection."""
        # Stop live data tracking (cancels WebSocket task)
        self._async_stop_live_data_tracking()
        
        # WebSocket sessions are automatically cleaned up when tasks are cancelled
        # since each connection creates and manages its own session

    @callback
    def async_update_options(self) -> None:
        """Update options and reconfigure live data tracking."""
        self.update_interval = self._get_update_interval()
        self._async_setup_live_data_tracker()
