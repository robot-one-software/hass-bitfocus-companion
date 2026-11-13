# Changelog

## 0.1.0 (unreleased)

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
