# hass-pvs

Home Assistant SunPower PVS Integration.

Based on https://github.com/krbaker/hass-sunpower
and https://www.home-assistant.io/integrations/enphase_envoy/


## Installation

1. Install Home Assistant on your target system https://www.home-assistant.io/installation/
2. Install HACS https://www.hacs.xyz/docs/use/download/download/
3. Add this Repo to HACS by going to the 3 dots on the right ...-> Custom repositories ->
 Repository: `SunStrong-Management/pvs-hass` Category: `Integration`
4. Install this integration in HACS searching for `PVS` (using the latest release is recommended)
5. Restart Home Assistant
6. In the Home Assistant UI go to "Configuration" -> "Integrations" click "+" and search for "PVS".
   * The main configuration is `<IP/Hostname>[:port]`. Check your router configuration for the IP of your PVS


## Options (available from 'configure' once integration is setup)

### Data update interval (seconds)

This sets how fast the integration will try to get updated info from the PVS.


## Devices

### Gateway

This is the data from the PVS Gateway device. These sensors provide diagnostic and usage information about the gateway itself.

| Entity           | Units   | Description                                 |
| ---------------- | ------- | ------------------------------------------- |
| `Uptime`         | Seconds | Time since the gateway was last restarted   |
| `RAM Usage`      | %       | Percentage of RAM currently in use          |
| `Flash Usage`    | %       | Percentage of flash storage currently used  |
| `CPU Usage`      | %       | Percentage of CPU currently in use          |

### Inverter

This is the data from each Micro Inverter.  Each inverter optimizes the power generation
using [MPPT][mppt] all of the panel side power data is reported from each inverter.
You should see one of these for every panel you have, they are listed by serial number.

| Entity            | Units  | Description                                                                  |
| ----------------- | ------ | ---------------------------------------------------------------------------- |
| `Frequency`       | Hz     | Observed AC Frequency.                                                       |
| `Lifetime Energy` | kWh    | Lifetime produced power from this panel / inverter                           |
| `Power`           | kW     | Power this panel is measuring                                                |
| `Voltage`         | Volts  | Voltage this panel is measuring (wired across both phases so seeing 240+-)   |
| `Current`         | Amps   | Electrical current this inverter is producing on the AC side                 |
| `Temperature`     | oC     | Temperature of this inverter                                                 |

### Meter

TBD

## Debugging

### Installed through HACS but I can't find it in the integrations list

Some people seem to have a browser caching / refresh issue it seems to be solved by completely
clearing caches or using another browser.

***
[mppt]: https://en.wikipedia.org/wiki/Maximum_power_point_tracking
[power-factor]: https://en.wikipedia.org/wiki/Power_factor
[sunpower-us]: https://us.sunpower.com/products/solar-panels
