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
- The largest room temperature deficit (`target - current`) determines a temperature offset. The original piecewise modulation curve is retained and capped by `delta_max`.
- The Atlantic target is the Atlantic climate's current temperature plus the offset, limited to 15–28 °C and rounded to 0.5 °C.
- If every room climate is available and none is in `heat`, the Atlantic climate is set to `off`.
- If room climates are unavailable, the integration leaves the Atlantic mode unchanged rather than treating missing data as an explicit demand to turn off.

Defaults are a 0.2 °C deadband, a 6 °C maximum offset, and a -0.5 °C offset when all active rooms are already at their target. These values can be changed in the integration options.

This is an initial, deliberately small controller. It does not implement an independent watchdog or a frost-protection fallback; those behaviors should be added in Home Assistant if required.
