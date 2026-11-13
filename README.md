# Bitfocus Companion for Home Assistant

Brings **[Bitfocus Companion](https://bitfocus.io/companion)** into Home Assistant.
Press any Companion button from an automation, a dashboard or a voice assistant,
and watch its live text and colour. The same buttons keep working on your Stream
Deck; Home Assistant is simply another surface looking at them.

Companion drives 700+ devices (ATEM switchers, RØDECaster consoles, OBS, vMix,
lighting desks, audio mixers, PTZ cameras and more). With this integration all of
them become reachable from Home Assistant through the Companion buttons you have
already built.

Built by the [Dashable](https://dashable.app) team. Community project, not
affiliated with Bitfocus.

---

## What you get

| In Home Assistant | What it does |
|---|---|
| **Services** `bitfocus_companion.press`, `down`, `up`, `rotate` | Act on any button by page / row / column. No setup per button. |
| **Button entities** | One per Companion button you choose. Pressing it presses the Companion button. |
| **Text sensors** | Live text of each chosen button, with background colour, text colour and pressed state as attributes. |
| **Variable sensors** | Any Companion variable you name (ATEM fader gain, RØDECaster levels, tally, timecode, custom variables), polled every second. |
| **Service** `bitfocus_companion.set_custom_variable` | Write a value into a Companion custom variable, for sliders and faders. |
| **Event** `bitfocus_companion_button` | Fired when a chosen button is pressed or released anywhere, Stream Deck included. Use it as an automation trigger. |
| **Connected** binary sensor | Per server, so you can see when Companion is down. |

Everything is local. Home Assistant talks to Companion over its satellite protocol
on your network; nothing goes through the cloud.

## What you need

- Home Assistant 2024.12 or newer with [HACS](https://hacs.xyz).
- Bitfocus Companion **4.3 or newer** (5.x recommended), reachable from the
  Home Assistant machine on TCP port 16622 (the satellite port, on by default).
- One switch flipped in Companion: **Settings → Protocols → Enable Button
  Subscriptions API**. Without it Companion refuses button access and the
  integration shows a Repairs notice telling you so.
- For **variables** only: Companion's HTTP API must be reachable from Home
  Assistant. Companion listens on localhost by default. In the Companion
  launcher window (the small Companion app, not the web settings) change the
  interface it listens on from 127.0.0.1 to the computer's network address or
  all interfaces. Port 8000 unless you changed it.

## Install

1. In HACS choose **Custom repositories**, add this repository as an
   **Integration**, then download **Bitfocus Companion**.
2. Restart Home Assistant.
3. Companion usually shows up on its own under **Settings → Devices & services**
   as a discovered device. Click **Configure**. If it does not appear, click
   **Add integration**, search for *Bitfocus Companion* and enter the host.

Several Companion servers can be added; each one is its own device.

## Choose buttons (optional)

Services work for any button without further setup. If you also want entities,
open the integration's **Configure** menu:

- **Add buttons from a page** scans one Companion page and lists every position
  that has something on it, labelled with its button text. Tick the ones you
  want; each is named after its text. Set the grid size if yours is not the
  default 4 rows by 8 columns.
- **Add one button by position** takes a page, row and column (rows and columns
  start at 0, pages at 1) and an optional name.

Each chosen button creates:

- `button.<server>_<name>` — press it to press the Companion button.
- `sensor.<server>_<name>_text` — the button's current text; attributes
  `background_color`, `text_color`, `pressed`, `font_size`, `button_type`.

## Variables (optional)

**Configure → Add variables**: choose the connection (the list comes from
Companion), paste one or more variable names, one per line, as they appear in
Companion's Variables tab, for example `combo1_livelevelL`. You can also paste
full references like `$(atem:program_input)` or `custom:fader`. Companion is
asked for each one before it is saved, so typos are caught immediately.

Give a unit (for example `dB`) when the values are numbers: the sensor then gets
a history graph and works in numeric conditions. Leave it blank for text such
as input names or timecode.

Each variable becomes `sensor.<server>_<connection>_<variable>` and is polled
every second (change this under **Configure → HTTP API settings**).

## Use it

Press a button from an automation or script:

```yaml
action: bitfocus_companion.press
data:
  page: 1
  row: 0
  column: 2
```

Hold and release (for buttons with separate press and release actions):

```yaml
action: bitfocus_companion.down
data: { page: 1, row: 0, column: 2 }
# ...later
action: bitfocus_companion.up
data: { page: 1, row: 0, column: 2 }
```

Turn an encoder button three steps left:

```yaml
action: bitfocus_companion.rotate
data: { page: 2, row: 0, column: 0, direction: left, amount: 3 }
```

With more than one Companion server, add `server: "<name as shown in Home Assistant>"`.

Write a custom variable (the usual way to drive a fader: a Companion Trigger
watches the variable and runs the device action):

```yaml
action: bitfocus_companion.set_custom_variable
data: { name: fader_level, value: -12.5 }
```

Trigger an automation from a Stream Deck press:

```yaml
triggers:
  - trigger: event
    event_type: bitfocus_companion_button
    event_data:
      page: 1
      row: 0
      column: 2
      pressed: true
```

### In Dashable (or any dashboard)

Give a dashboard button the tap action **Perform action** →
`bitfocus_companion.press` with the page, row and column, or point it at the
`button.` entity you created. Bind the button's label or colour to the matching
`sensor.…_text` entity and its `background_color` attribute to mirror Companion's
feedback.

## Troubleshooting

- **Repairs says "Enable button subscriptions"**: in Companion open
  **Settings → Protocols** and turn on **Enable Button Subscriptions API**.
  Home Assistant reconnects by itself.
- **Not discovered**: Companion announces itself over mDNS from version 5.0.
  Add it manually with the host or IP. If Home Assistant runs in Docker, mDNS
  needs host networking.
- **"Could not reach Companion's satellite port"**: confirm Companion is running
  and that port 16622 is not firewalled. The satellite listener is on by default
  under **Settings → Protocols**.
- **Entities unavailable**: the server is disconnected or subscriptions are off.
  The **Connected** sensor tells you which.
- **Variable sensors unavailable / "HTTP API is not reachable"**: Companion's
  web interface is bound to localhost. Change the interface in the Companion
  launcher window, or fix the port under **Configure → HTTP API settings**.

## Not yet included

Companion has no API that lists variables, so they are chosen by name rather
than from a full picker. A per-module suggestions catalogue and push updates
faster than one per second are on the list.

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/python -m pytest
```

The tests run against a small fake Companion server (`tests/conftest.py`), so no
real Companion is needed.

## License

MIT. Companion is a trademark of Bitfocus AS.
