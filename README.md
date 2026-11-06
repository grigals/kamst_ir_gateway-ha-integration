# Kamst-IR Gateway for Home Assistant

A custom integration for the Smart Gateways **Kamst-IR** gateway (optical
reader for Kamstrup heat and water meters). Each gateway is added from the UI
and becomes its own Home Assistant device. It replaces the hand-written MQTT
YAML packages.

- **REST polling** (default) or **MQTT push**, chosen per gateway.
- **Several gateways**: add the integration once per gateway.
- Heat energy works in the **Energy dashboard** (GJ, `total_increasing`).
- Firmware **update entity**. Install is available in MQTT mode.
- Diagnostics download, reconfigure flow, English and Latvian translations.

Requires Home Assistant 2025.12 or newer. It has no extra Python dependencies.

> [!WARNING]
> **Unofficial project, use at your own risk.** This integration is not
> affiliated with, endorsed by or supported by Smart Gateways (smartgateways.nl),
> Kamstrup or the Home Assistant project. It was written from the gateway's
> public behaviour and documentation, not from any vendor specification.
> Product names and trademarks belong to their owners and are used only to say
> what this integration works with.
>
> The *Restart* button and the firmware *Install* action send commands to your
> gateway. Readings are for information only and must not be used for billing.
> The software is provided "as is", without warranty of any kind; see
> [LICENSE](LICENSE).

## Installation

### HACS (recommended)

1. In HACS, open the menu → **Custom repositories**, then add
   `https://github.com/grigals/kamst_ir_gateway-ha-integration` with the
   category **Integration**.
2. Search for **Kamst-IR Gateway** and install it.
3. Restart Home Assistant.

### Manual

Download `kamstir.zip` from the latest
[release](https://github.com/grigals/kamst_ir_gateway-ha-integration/releases),
extract it to `config/custom_components/kamstir/` and restart Home Assistant.

## Setup

**Settings → Devices & services → Add integration → Kamst-IR Gateway**

| Field | Notes |
| --- | --- |
| Host | IP address or hostname of the gateway |
| Port | Default `82` |
| Name | Device name, e.g. `Kamstrup K1`. Entity IDs follow it: `sensor.kamstrup_k1_heat_energy` |
| Meter type | Heat or water. Decides which sensors are enabled by default |
| Connection mode | REST polling or MQTT push |

The gateway has to answer `http://<host>:82/kamst-ir/api/read` during setup.
That's how the integration gets the MAC address it uses as the device ID.

### MQTT mode

MQTT mode needs the Home Assistant **MQTT integration** to be set up and
connected to the same broker as the gateway. After the first step you're asked
for the **topic prefix**. That's the prefix set in the gateway's web UI under
MQTT. It's the part of the topic before `/reading/…`. If the gateway's PREFIX
field is blank, it's the default `kamstir`. With a prefix set, it looks like:

```
home/heating/kamstir
```

The gateway publishes to `<prefix>/reading/<field>` and
`<prefix>/smart_gateways/<field>` every 10 seconds. Install firmware publishes
`yes` to `<prefix>/smart_gateways/install_update`. Each gateway needs its own
prefix, so set one in the web UI of every gateway after the first. The setup
refuses a prefix that another gateway already uses.

The REST API needs no login, and the vendor says it can't be password-protected.
The `admin` / `smartgateways` login only applies to the gateway's web UI, which
this integration doesn't use.

### Options and reconfigure

- **Configure** sets the scan interval (30–3600 s, default 60) and lets you
  switch between REST and MQTT or change the prefix. The gateway reads the
  meter optically at its own interval, so polling faster than that adds nothing.
- **Reconfigure** changes the host, port or prefix without losing entities or
  history. It refuses an address that belongs to a different gateway.

## Entities

The main sensors are heat energy (GJ), power (kW), supply and return
temperature (°C), temperature difference (K), flow (L/h), volume (m³) and
operating hours. There are also month and year min/max/average statistics,
diagnostics (WiFi signal, last restart, info code), a *Connected* binary sensor,
the firmware update entity and a **Restart** button. The button calls the
gateway's `/reboot` page and works in both REST and MQTT mode.

For a **heat** meter the heat sensors are enabled by default. For a **water**
meter, volume, flow, water temperature, leak/burst and battery are enabled
instead. You can enable any other entity on the device page.

**Units.** Power is instantaneous power in **kW**. The vendor YAML labels it
kWh, but Kamstrup meters report kW, and the vendor's own dashboard gauge shows
kW. Heat energy is reported in **GJ**. To see it in kWh or MWh, change the
display unit in the entity settings; HA converts the value and keeps the
statistics.

**Dates.** The month/year min/max date fields are date sensors. *Meter clock*
combines `device_date` and `device_time` into one timestamp, in Home
Assistant's time zone. The gateway sends dates as YYMMDD, sometimes with a
`.00` suffix (`261101.00` = 1 Nov 2026), and times as hhmmss (`150000`).
YYYYMMDD and separated forms like `06-11-2026` are also understood. Diagnostics
show the raw strings if a value can't be parsed.

*Battery days left* is sent as text (`"2 years and 45 days"`) and shown in days.
Meters on mains power report 0.

**Energy dashboard.** The Energy dashboard has no separate category for district
heating. Add *Heat energy* under **Gas consumption** (it accepts energy sensors),
and *Volume* of a water meter under **Water consumption**.

A value the meter hasn't sent yet (`""` or `"-"`) shows as *unknown*. When the
gateway can't be reached, entities show *unavailable* and *Connected* turns
off. In MQTT mode that happens after 10 minutes without data.

## Migrating from the YAML packages

1. Add each gateway through the UI as described above.
2. Delete the old packages file (for example `packages/kamstir_k1.yaml`) and
   restart Home Assistant.

While the old YAML entities still exist, a repair issue reminds you about this.
The integration never deletes anything itself. If you want the new entities to
keep the old entity IDs, rename them on the entity settings page after the old
ones are gone. Dashboards (including apexcharts cards) keep working once the
entity IDs match.

## Development

```bash
python -m pip install -r requirements_test.txt
python -m pytest
```

Home Assistant's test harness doesn't run on Windows; use WSL or Linux.

### Releasing

1. Bump `version` in `custom_components/kamstir/manifest.json` and commit.
2. Tag and push: `git tag v0.1.1 && git push origin v0.1.1`.

The **Release** workflow checks that the tag matches the manifest, builds
`kamstir.zip` and publishes the GitHub release that HACS installs from. Tags
like `v0.2.0b1` become pre-releases.
