# Kamst-IR Gateway for Home Assistant

A custom integration for the Smart Gateways **Kamst-IR** gateway (optical
reader for Kamstrup heat and water meters). Each gateway is added from the UI
and becomes its own Home Assistant device. It replaces the hand-written MQTT
YAML packages.

- **REST polling** (default) or **MQTT push**, chosen per gateway.
- **Several gateways**: add the integration once per gateway.
- Heat energy works in the **Energy dashboard** (MWh, GJ or kWh, `total_increasing`).
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
| Energy unit | The unit on the meter display: MWh (default), GJ or kWh |
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
- **Configure** also turns on [cost tracking](#cost-tracking).

## Entities

The main sensors are heat energy (MWh, GJ or kWh), power (kW), supply and return
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
kW. Heat energy is sent in whatever unit the meter is configured for, so set
**Energy unit** (MWh, GJ or kWh; default MWh) to the unit on the meter display,
when adding the gateway or later under *Configure*. MWh and GJ are shown with
3 decimals and kWh as whole numbers, like the meter. To show the value in a
different unit, change the display unit in the entity settings; HA converts it.

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

## Cost tracking

Cost tracking is optional and off by default. Turn it on under *Configure*. It
adds a cost sensor that multiplies what the meter counts by a price: *Heat
cost* for a heat meter (from *Heat energy*), *Water cost* for a water meter
(from *Volume*). The cost is in Home Assistant's currency.

### The price

Without a price entity the integration keeps the price itself. Three entities
appear under *Configuration* on the device page:

| Entity | What it is |
|---|---|
| *Heat price* | The current price, per unit of the meter reading (for example EUR/MWh). Enter it including VAT. |
| *Next heat price* | The price of the next tariff. |
| *Next heat price from* | The day the next tariff starts. |

At midnight on that day *Next heat price* becomes *Heat price* and the other
two are cleared. Both must be set for that to happen. A water meter has the
same three with *water* in the name and a price per m³.

If you already have the price as an entity, pick it as **Price entity** under
*Configure*. Sensors, numbers and number helpers work. The three entities above
are then not created. A unit like `EUR/kWh` is converted to the meter's unit;
without a unit the price is taken as per unit of the meter reading. While the
price entity is unavailable its last value is used.

### How the cost is counted

Each new meter reading adds *(reading − previous reading) × price*.

- What is used while no price is set costs nothing, also not later.
- What is used while Home Assistant is off or the gateway can't be reached is
  priced when the next reading arrives, at the price of that moment. If a
  price change fell in between, the amount is split by time.
- Changing the price never changes cost that was already counted.
- A reading lower than the last one is ignored. After three in a row it is
  taken as a new meter, and counting carries on from there.
- Fixed fees are not included.

The sensor's state is the cost since cost tracking was turned on. Turning cost
tracking off, or switching to a price entity, leaves the entities it no longer
uses in place; delete them on the device page if you don't want them.

### History: the backfill action

The cost sensor only counts from the moment it exists. The meter reading
usually has statistics that go back further. The **Backfill cost** action
(`kamstir.backfill_cost`) prices those and writes them as the cost sensor's
history.

1. Turn on cost tracking and set the price.
2. Open *Developer tools → Actions*, choose **Kamst-IR Gateway: Backfill cost**
   and pick the gateway.
3. Press *Perform action*. The response shows the period, the amount used and
   the cost that was written.

```yaml
action: kamstir.backfill_cost
data:
  device_id: <the gateway>
  price: 85            # optional, default: the current price
  start: "2026-01-01"  # optional, default: the oldest statistics
  end: "2026-11-26"    # optional, this day is not included
```

Hours outside `start`–`end` keep the cost they had, and running the action
again replaces what it wrote. So for several tariffs, run it once per period
with that period's price. Without `end` it prices up to the last full hour.

Things to know:

- The history is in the long-term **statistics** (hourly), not in the state
  history. Use statistics cards (*Statistics graph*, *Statistic*, or
  apexcharts-card with `statistics:`) and show the **change** per period.
- The sensor's state does not jump: it stays the cost since tracking was turned
  on. For the same reason the `sum` of the backfilled hours is below the
  sensor's and usually negative. Only the differences between rows mean
  anything, and those are the cost of each hour.
- It needs at least two hours of statistics of the meter sensor, and goes back
  as far as those do.

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
