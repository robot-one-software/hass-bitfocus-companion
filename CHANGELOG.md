# Changelog

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
