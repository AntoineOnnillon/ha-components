# Atlantic heat-pump controller

This Home Assistant custom integration moves the room-demand calculation out of the ESPHome `atlantic_climate` component. The ESPHome climate remains the direct interface to the Atlantic bus: Home Assistant sends it an HVAC mode and target temperature, and ESPHome continues publishing the Atlantic setpoint and measured ambient temperature.

## Installation

### HACS

In HACS, add `AntoineOnnillon/ha-components` as a custom repository with the
category **Integration**, then install **Atlantic Heat Pump Controller**.
Restart Home Assistant and add the integration from **Settings → Devices &
services → Add integration**.

### Manual

Copy `custom_components/atlantic_heat_pump` into the `custom_components`
directory of Home Assistant, then restart Home Assistant. Add **Atlantic Heat
Pump Controller** from **Settings → Devices & services → Add integration**.

Select:

- the Atlantic climate exposed by ESPHome;
- one or more room climates exposed by the TRL valves.

The Atlantic climate must expose a `current_temperature` attribute, which is used as the reference temperature. Room climates must expose `current_temperature` and `temperature` attributes. The integration considers a valve active when its HVAC mode is `heat`.

## Initial control behavior

- If at least one room climate is in `heat`, the Atlantic climate is set to `heat`.
- The largest room deficit (`target - current`) among heating rooms determines a positive offset through the modulation curve. If every heating room is above its target, the least negative difference is applied directly as a negative offset. Differences within the deadband produce no offset, and `delta_max` caps the offset in either direction.
- The Atlantic target is the Atlantic climate's current temperature plus the offset, limited to 15–28 °C and rounded to 0.5 °C.
- If every room climate is available and none is in `heat`, the Atlantic climate is set to `off`.
- If room climates are unavailable, the integration leaves the Atlantic mode unchanged rather than treating missing data as an explicit demand to turn off.

Defaults are a 0.2 °C deadband and a 6 °C maximum absolute offset. These values can be changed in the integration options.

The integration also creates a diagnostic device with sensors for the Atlantic current temperature, the largest TRV temperature difference, the applied offset, the calculated Atlantic target, and the number of TRVs in heating mode. Difference, offset, and calculated target are unavailable when no valid heating demand can be calculated.

This is an initial, deliberately small controller. It does not implement an independent watchdog or a frost-protection fallback; those behaviors should be added in Home Assistant if required.

# Cumulus PV controller

The **Cumulus PV Controller** integration moves photovoltaic scheduling and heater modulation from the ESPHome `cumulus` component to Home Assistant. Home Assistant reads the tank temperature, PV/grid power and optional forecasts, then periodically writes a 0–100% heater command to an ESPHome number entity.

## Installation

Install **Cumulus PV Controller** from this repository in HACS, or copy
`custom_components/cumulus_pv` into Home Assistant's `custom_components`
directory and restart Home Assistant. Add the integration from **Settings →
Devices & services → Add integration**.

The ESPHome device must expose the following `cumulus` entities:

- a top-of-tank temperature sensor;
- the `heater_command` number platform, with the command number exposed to HA;
- the `enable`, `boost` and `pv_priority` switches.

For example:

```yaml
cumulus:
  id: water_heater
  heater_output: water_heater_output
  top_temperature: tank_top_temperature
  target_temperature: 55
  min_temperature: 40
  max_temperature: 65

number:
  - platform: cumulus
    cumulus_id: water_heater
    heater_command:
      name: "Water heater command"

switch:
  - platform: cumulus
    cumulus_id: water_heater
    enable:
      name: "Water heater enable"
    boost:
      name: "Water heater boost"
    pv_priority:
      name: "Water heater HA PV control"
```

Select the daily PV forecast sensors provided by your preferred forecast
integration; the forecast values are expected in kWh. At least one current PV
surplus or grid-export sensor is also required. Grid import, remaining
household consumption, peak hour and measured heater power are optional. Power
sensors are expected in W, with export/import positive in their named
direction.

When migrating an existing ESPHome configuration, remove the former PV input
sensors and planner settings from the `cumulus:` block. Configure those source
entities and strategy values in this integration instead. Local telemetry
(heater output/power, stored energy, safety counters, pump and legionella
diagnostics) remains available from ESPHome.

## Control and fallback

The integration refreshes the ESPHome number command every 15 seconds. The
ESP32 remains responsible for the physical output, over-temperature cutoffs,
minimum-temperature thermostat fallback, manual boost, anti-legionella cycle,
pump control and sensor watchdog. If HA stops refreshing the command, ESPHome
falls back to its local thermostat after the configured `external_duty_timeout`
(two minutes by default). Turning off the `pv_priority` switch returns control
to the local thermostat; unloading the HA integration also turns that switch
off.

PV burst/modulation, the energy reserve and peak-window scheduling are
calculated in Home Assistant. The planner accepts daily PV production forecasts
for today through six days ahead and uses a configurable horizon of up to seven
days. Daily water-heater consumption is learned automatically from complete
days, prioritizing the cumulative heater energy meter when configured. Without
that meter, the integration sums measured heater power over time; if that is
unavailable, it estimates use from the configured element power while a manual
boost or HA PV control is active. Until a complete day of measurements is
available, the configured daily-consumption value is used as a fallback. The
`Daily water heater consumption forecast` diagnostic shows the value used by
the planner. The planner reduces PV production forecasts by a configurable
safety margin, then compares each day with estimated household use and
hot-water energy. It only raises the target when the forecast shows a future deficit that
can be usefully covered by stored heat; forecast surpluses before a deficit are
accounted for, as are estimated daily tank losses. Adequate forecasts leave the
base target unchanged, avoiding needless overheating and standing losses.

Extra heat is requested only while actual PV surplus is available. If PV is
insufficient after the production window, grid catch-up remains limited to the
normal base target. The optional economic gate compares the configured value
of avoided future energy with the value of exported energy after estimated
tank losses. Defaults reflect a 50% transport charge avoided on recovered
energy and no separate export payment; adjust both percentages to match the
actual contract. The default forecast margin is 15%, and estimated tank loss
is 5% per day; these are tunable assumptions, not measured properties.

The diagnostic **Estimated thermal charge** uses the top temperature as a
proxy: 0% corresponds to the configured minimum temperature, 100% to the base
target, and 150% to the maximum planned boost temperature (bounded by the HA
maximum). It is an estimate, not a literal volume/energy measurement, because
a single top sensor cannot measure tank stratification.

The optional whole-home energy meter must include the water heater; the optional
water-heater meter is used to separate its daily consumption from the household
load. With both cumulative energy meters configured (Wh, kWh or MWh), Home
Assistant learns rolling daily averages from their changes, retaining up to
seven completed days. Until enough history is available, the configurable daily
home and water-heater estimates are used. A forecast of today's remaining
household consumption excluding the water heater can refine today's estimate.
The HA maximum tank temperature should not exceed
the `max_temperature` configured in ESPHome.
