# Changelog

## 0.5.0

- **Add buttons (pick from Companion).** Lists every real button Companion
  has across all pages that is not in Home Assistant yet, labelled with page,
  position and text, with page navigation buttons left out. Tick to add; names
  come from the button text.
- **Rotary buttons.** Buttons with rotary actions are detected and marked
  `rotary: true`; each also gets *rotate left* and *rotate right* button
  entities, so a dial on a dashboard can turn Companion encoders. Existing
  buttons learn their rotary flag on the first start after the update.
- The page scan no longer asks for the grid size; it is now the fallback for
  installs where the admin API cannot be read and covers up to 8 rows by 16
  columns on its own.

## 0.4.1

- Fix: the page-map watch dropped and reconnected every 30 seconds, because
  Companion's admin API sends a plain-text keepalive `PING` that was not being
  answered. This also made the "page map is not reachable" Repairs notice
  appear even though moves were being followed. The watch now answers the
  keepalive, and the notice only appears after the watch has been down for
  90 seconds.

## 0.4.0

- **One entity per button.** The button entity now carries the live text,
  first-line label, colours, pressed state, type, position and control id as
  attributes. The text sensor is off by default; turn it on under *Configure →
  Entity settings* if you want text history. Existing text sensors are removed
  unless that option is on.
- **Moved buttons are followed.** Each button is tracked by Companion's stable
  control id, read from the page map. Move a button to another position or page
  in Companion and the Home Assistant entity, its history and your dashboard
  bindings carry on; the integration resubscribes and updates the stored
  position by itself. Existing buttons are migrated in place on upgrade. A
  deleted button becomes unavailable with `exists: false`.
- A Repairs notice explains when the page map cannot be read (then buttons are
  tracked by position only).

## 0.3.0

- **Pick variables from a list.** *Configure → Add variables* now shows the
  connections Companion knows, with variable counts, then a searchable list of
  that connection's variables with their current values and descriptions. Tick
  the ones you want. A filter box narrows very large connections (Companion's
  own Home Assistant connection can have thousands). The list comes from the
  same internal API Companion's admin page uses; it is read only while you are
  picking, never at runtime.
- The paste-a-name route stays as *Add variables by name*.

## 0.2.1

- *HTTP API settings* gains an optional **HTTP host**, for Companion installs
  whose web interface listens on a different address than the satellite
  connection uses (a VPN address, for example). Errors now show the exact URL
  that was tried.

## 0.2.0

- **Variables.** Pick Companion variables by name under *Configure → Add
  variables* and each becomes a sensor, polled over Companion's HTTP API (every
  second by default, adjustable). Give a unit such as dB for numeric sensors with
  history graphs. Custom variables work too (`custom:name`).
- New service `bitfocus_companion.set_custom_variable` for writing values back,
  the building block for sliders and faders.
- *Configure → HTTP API settings* sets the port and poll interval and tests the
  connection. A Repairs notice explains when Companion's HTTP API is not
  reachable (Companion listens on localhost unless its launcher says otherwise).

## 0.1.1

- Fix: the connection to Companion dropped and reconnected every few seconds.
  Companion closes a satellite socket that is silent for 5 seconds; the
  keepalive now runs every 2 seconds.
- Entities no longer flip to unavailable on a brief drop; they wait 5 seconds
  for the reconnect first. The Connected sensor still reflects the socket at once.
- New **Add buttons from a page** option: scan a Companion page and tick the
  buttons to add, named after their text, instead of entering positions one by one.
- Fix: a button could be subscribed twice during startup.

## 0.1.0

First version, built for testing.

- Discovers Companion servers on the network (mDNS) or adds them by address.
- Presses, holds, releases and rotates any Companion button by page/row/column
  through the `bitfocus_companion.press`, `down`, `up` and `rotate` services.
- Optional button entities (press) and text sensors (live text, colours,
  pressed state) for buttons you pick in the integration's options.
- A `bitfocus_companion_button` event whenever a watched button is pressed or
  released from anywhere, including a Stream Deck.
- Connectivity sensor per server, automatic reconnect, and a Repairs notice
  when Companion's Button Subscriptions API is switched off.
