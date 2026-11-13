# Dashable × Bitfocus Companion — feasibility research

Researched 2026-11-12 against Companion v5.1.2 (current stable, released 4 Nov 2026) and the Satellite API spec at https://companion.free/for-developers/Satellite-API.

## Verdict in one paragraph

Feasible, and Companion has already built the exact hooks a third-party touchscreen surface needs. The **Satellite API** (TCP 16622, WebSocket 16623) lets Dashable register as a surface, receive fully rendered button images, send presses/rotations, **subscribe to any individual button anywhere in Companion** (not just a grid), and exchange **typed variables in both directions** so Companion pushes device state (fader levels, tally, timecode) into Dashable widgets and Dashable can push slider values back. Nothing has to be built on Companion's side. The hard parts are product/UX (sliders don't exist in Companion's model, so writes need a small Trigger per control) and network reality (plain ws:// from an https-served web viewer is blocked by browsers). Difficulty: medium. A credible first version is a few weeks of work on the Dashable side.

## 1. How Companion exposes itself (what we can plug into)

| Path | Direction | Push or poll | Auth | Best use for Dashable |
|---|---|---|---|---|
| **Satellite API** (TCP 16622 / WS 16623) | both | push | none | Primary integration. Buttons, subscriptions, variables. |
| Legacy HTTP API (`/api/location/...`, `/api/variable/<label>/<name>/value`, `/api/custom-variable/...`) | both | poll only | none | Fallback for reading a one-off variable or pressing a button. |
| New REST API `/api/v2` (5.1, "prototype") | read + brightness | poll | Bearer token (`cpn_...`), scopes | Not useful yet. Surfaces and connections only; no variables, no streaming. |
| TCP/UDP, OSC (port 12321), RossTalk, Ember+, Art-Net | write | n/a | none | Not needed. |
| Companion **Triggers** ("On variable change" → any action, incl. Generic HTTP/OSC send) | out of Companion | push | n/a | How a Dashable slider value becomes a device action. |
| Surface plugins (`companion-surface-*`, `@companion-surface/base`) | runs *inside* Companion as a Node plugin | n/a | n/a | Alternative architecture; see §4. |

Key facts from the Satellite spec (API v1.14 ships in Companion 5.1; v1.15/1.16 land in 5.2):

- **Framing:** one line per message, `COMMAND KEY=VAL KEY="quoted val"`. `BEGIN CompanionVersion=.. ApiVersion=..` then `CAPS SUBSCRIPTIONS=1 NONSQUARE=1 BITMAP_FORMATS=rgb,png,webp ...`. PING/PONG every ~2 s. 2 MB line limit.
- **ADD-DEVICE** with either a simple grid (`KEYS_TOTAL`, `KEYS_PER_ROW`, `BITMAPS=<px>`, `COLORS`, `TEXT`, `TEXT_STYLE`) or an advanced `LAYOUT_MANIFEST` (base64 JSON) with per-control style presets, non-square bitmaps `{w,h}`, LEDs, and arbitrary row/column placement. `DEVICE-APPEARANCE` (5.2) lets a surface describe its physical face so Companion's UI can draw it.
- **KEY-STATE** delivers `BITMAP` (raw RGB, or lossless PNG/WebP data URL since 5.0), `COLOR`, `TEXTCOLOR`, `TEXT`, `FONT_SIZE`, `PRESSED`, `TYPE=BUTTON|PAGEUP|PAGEDOWN|PAGENUM`, and `LOCATION=page/row/col`. Bitmap caching (`BITMAP_ID`, `BITMAP-CACHE`, `BITMAP-EVICT`) arrives in 5.2.
- **Button Subscriptions** (API 1.10, Companion 4.3+, must be enabled in Settings → Protocols → "Enable Button Subscriptions API"): `ADD-SUB SUBID=.. LOCATION=1/2/3 BITMAP=96 ...` → stream of `SUB-STATE`, and `SUB-PRESS` / `SUB-ROTATE` to act. The spec says this exists for "custom UIs, secondary displays". This is the mechanism for *a Companion button as a free-floating Dashable widget*.
- **Variables:** `ADD-DEVICE ... VARIABLES=<base64 JSON [{id,type:"input"|"output",name,description}]>`.
  - `output` = Companion → surface. The user enters a Companion expression (e.g. `$(atem:audio_input_1301_faderGain)`) in the surface's settings and Companion pushes `VARIABLE-VALUE DEVICEID=.. VARIABLE=.. VALUE=<base64>` on every change. (Surface-dev docs: "the user provides a Companion expression that drives the value, and Companion pushes the result to your surface.")
  - `input` = surface → Companion via `SET-VARIABLE-VALUE`. The user stores it in a custom variable and uses it in Triggers/actions. (Docs' example is a T-bar.)
  - `ENCODING=json` for typed values arrives in API 1.16 (Companion 5.2); until then values are strings.
- **Paging:** surfaces follow Companion's page model. `CAN_CHANGE_PAGE` + `CHANGE-PAGE DIRECTION=1|0`. Page-permission groups apply.
- **Lock screen:** `PINCODE_LOCK=FULL|PARTIAL`, `LOCKED-STATE`, `PINCODE-KEY`. While locked Companion sends no drawing data.
- **Discovery:** Companion 5.0 announces satellite ports over mDNS.

## 2. Mapping to Dashable concepts

| Dashable widget | Companion mechanism | Notes |
|---|---|---|
| **Companion Button** (single) | `ADD-SUB` at a location; render the PNG/WebP; `SUB-PRESS` on touch | Non-square sizes OK (`STYLE.bitmap {w,h}`). Companion does the whole look (text, icons, feedback colours). Zero config beyond page/row/col. |
| **Companion Page / Grid** | `ADD-DEVICE` with a simple or `LAYOUT_MANIFEST` grid | Appears in Companion's Surfaces list like a Stream Deck; user assigns it a page; follows page changes; honours page permissions and PIN lock. |
| **Value / gauge / meter** (e.g. ATEM fader gain, RCV master level, timecode) | one `output` variable per widget; user binds an expression in Companion | Push, not poll. Any `$(label:var)` or full expression. |
| **Slider / fader** (write) | `input` variable → custom variable → Trigger "on variable change" → module action (e.g. ATEM "Fairlight audio: set fader gain" with expression) | Works, but it is a 3-step setup in Companion per control. See gotcha 1. |
| **Encoder / dial** | `SUB-ROTATE` / `KEY-ROTATE DIRECTION=±n` | Signed step magnitude needs API 1.14 (`ROTARY_AMOUNT`). Natural fit for Dashable dials. |
| Tally / state badge | `output` variable bound to a boolean expression | Many modules expose state only as *feedbacks*, not variables (RCV program/preview, record/stream state). Workaround: user wraps the feedback in a Companion button or custom variable. |

### What the two named devices actually expose

- **ATEM (companion-module-bmd-atem, v4.5, needs Companion 5.0+):** very mature, 76★. Variables cover program/preview input IDs and names, Fairlight master/monitor gain, per-input fader gain (seen in the wild as `$(atem:audio_input_1301_faderGain)`, -100..+10 dB, exact IDs to be confirmed against `src/variables/`), tally, timecode, recording/streaming active, record time remaining, media players, key/mask, multiviewer sources, SuperSource, aux. Actions include Fairlight audio input control, and v4.0 added expression support to "many feedbacks and actions". Good candidate for a slider demo.
- **RØDECaster Video (companion-module-rode-rcv):** young (12★, version 0.0.0, maintainers listed as RØDE and one community dev), OSC over network. Variables include `$(RCV:<channel>-<mix>_setlevel)` and `_setgain` (dB), `master_live_L/R`, timecodes, wireless mic battery/signal/mute, storage. Program/preview, record and stream *state* are feedbacks only, not variables. Live meter polling is commented out in `src/index.ts`, so meters may not update. Audio Sources action controls level/gain/mute. Workable for level sliders; expect rough edges.

## 3. Gotchas and risks

1. **Companion has no slider primitive.** Buttons and encoders are the only inputs in its model. A Dashable slider must round-trip: `SET-VARIABLE-VALUE` → custom variable → Trigger → action. Users must build that Trigger themselves (or Dashable ships an importable Companion config snippet). Also no debounce guidance: a slider emitting 60 updates/s will fire 60 actions; Dashable should throttle to ~10 Hz and send on release. Until API 1.16 (5.2) values are strings.
2. **Browser mixed-content.** The satellite WebSocket (16623) is plain `ws://`. A viewer served from `https://my.dashable.app` cannot open it (Chrome/Safari block insecure WS from secure origins). Options: native iOS app talks TCP/WS directly (fine); the HA-panel build is only fine if HA itself is served over http on the LAN; otherwise a tiny local bridge (or a Companion-side HTTPS reverse proxy) is needed. This is the same local-first problem Dashable already solves for Home Assistant, so the pattern exists.
3. **No security on any Companion remote protocol.** Satellite, HTTP, TCP, OSC all accept anything that can reach the port; the admin password explicitly "does not protect the remote control protocols". The 5.1 REST API has tokens but no useful surface yet. Dashable should treat Companion as LAN-only and never relay through the cloud.
4. **Rendered bitmaps, not semantics.** Companion sends pictures. Dashable cannot restyle a button's font or theme it; it can only size it. That is also a feature: feedback colours, icons and text all "just work". If you want Dashable-native styling you'd read individual variables instead.
5. **Bandwidth on large grids.** A 8×4 page at 96 px WebP is fine; a 20-button wall at 200 px with fast-changing timecode text is many KB/s per client. Bitmap caching (5.2) helps; until then keep sizes modest and prefer variables for text.
6. **Page semantics.** A satellite *surface* follows Companion's page model (one page at a time, page up/down, surface groups, page permissions). A Dashable page mixing buttons from several Companion pages should use *subscriptions*, not a surface. Subscriptions must be enabled by the user in Settings → Protocols.
7. **Version spread.** Subscriptions need Companion ≥4.3, PNG/WebP ≥5.0, signed rotary and LEDs ≥5.1, JSON variables and bitmap cache ≥5.2. Negotiate via `CAPS`/`ApiVersion` and degrade gracefully. Companion 5.x requires macOS 13.5+, so some installs sit on 4.x.
8. **Variable discovery.** No API enumerates module variables. Dashable cannot offer a picker of "all ATEM variables"; the user types the `$(label:var)` expression (same as they do in Companion today). You could ship presets per popular module.
9. **Many device states are feedbacks, not variables.** Especially in younger modules (RCV). Users may need a Companion button or custom variable as an intermediary for e.g. "is recording".
10. **Licensing.** Companion core is MIT-style open source (GitHub shows "Other"; check LICENSE.md). Talking to it over the network has no licensing implication. Embedding or bundling Companion would. Surface plugins (§4) would live in the Bitfocus org or your own repo under MIT.

## 4. Architecture options

| Option | Effort | Pros | Cons |
|---|---|---|---|
| **A. Dashable as a Satellite client** (recommended) | Medium: protocol client (~1–2 k lines), 3–4 widget types, settings UI, reconnect/caps negotiation | No Companion-side code; works with Companion 4.3+; push everywhere; subscriptions give free placement | Mixed-content issue for web viewer; slider write path is indirect |
| B. Surface plugin inside Companion (`@companion-surface/base`) plus a Dashable transport | High: Node plugin, Companion review/distribution, still need a Dashable-side client | Could present Dashable as a first-class surface with custom control kinds, auto-discovery | Two codebases; plugin API young (v1.4); no clear win over A since A already reaches the same features |
| C. Legacy HTTP polling only | Low | Trivial | Poll-only, no images, no subscriptions, 1 request per variable. Not competitive. |
| D. Embed Companion's "Web buttons"/Emulator page in an iframe | Very low | Weekend job | Not a surface, no page following, can't mix with Dashable widgets, no variables. Only a stop-gap. |

Suggested phasing for A: (1) Button widget via subscriptions + value widget via output variables; (2) Page/grid widget as a registered surface with PIN lock and page following; (3) Slider/encoder writes with an importable Trigger template and debounce; (4) 5.2 features (JSON variables, bitmap cache, appearance).

## 5. Audience: does this open a new market?

Yes, and it is a large, adjacent one. Bitfocus claims **300,000+ Companion users** and **15,000+ forum members**; the GitHub repo has ~2.4 k stars, 600 forks, contributors from 91 organisations, 700+ supported devices. Users are live-streamers, houses of worship, schools, corporate AV, esports, theatre and broadcast. Almost none of them are Home Assistant users today, so this is net-new reach rather than cannibalisation.

What they use for touchscreens now: Companion's built-in Emulator/Web-buttons page (plain grid, no sliders, no mixing with other data), Companion Satellite on a Raspberry Pi with a Stream Deck, iPad Stream Deck Mobile (Elgato-only, not Companion), TouchPortal / Macro Deck / Deckboard (PC macros, not broadcast gear). There is no polished, designable touch dashboard that combines Companion buttons with live meters, gauges and sliders. That is Dashable's pitch: *the pretty, big-screen operator panel for Companion*.

Where Dashable has an unfair advantage: the drag-and-drop editor, non-grid layouts, gauges/charts/clocks, media widgets, multi-screen sync, and (for the hybrid venue/studio) Home Assistant lighting/HVAC/power on the same panel as the switcher. Church tech teams and small studios already run both.

Risks on the market side: Companion's culture is open source and free; a paid Dashable tier needs to feel like a surface purchase (like buying a Stream Deck), not a toll. Bitfocus is responsive to surface vendors; an early conversation with Julian Waller (lead maintainer) about listing Dashable as a supported surface would help, and would surface any undocumented behaviours in the variable-binding UI.

## 6. Open questions to verify before building

- Exact UI label/placement of satellite variable binding in Companion 5.1 (docs confirm the behaviour, not the screen).
- ATEM Fairlight fader variable IDs and whether the gain action accepts an expression (changelog says many do).
- Whether RCV live meters actually update (code is commented out).
- Whether the Dashable viewer stack can open raw TCP/WS from each platform (iOS native yes; HA panel depends on HA's scheme; web editor no without a bridge).
- Companion 5.2 release timing for JSON variables and bitmap caching.

## Sources

- Satellite API spec: https://companion.free/for-developers/Satellite-API
- Surface development, input & variables: https://companion.free/for-developers/surface-development/surface-basics/input
- Surface plugin framework: https://github.com/bitfocus/companion-surface-api
- HTTP remote control (v5.1): https://companion.free/user-guide/v5.1/remote-control/http-remote-control
- REST API (v5.1): https://companion.free/user-guide/v5.1/remote-control/rest-api
- Settings / security: https://companion.free/user-guide/v5.1/config/settings , https://companion.free/user-guide/v5.0/security
- Triggers: https://companion.free/user-guide/v5.0/config/triggers
- Releases: https://github.com/bitfocus/companion/releases
- ATEM module: https://github.com/bitfocus/companion-module-bmd-atem
- RØDECaster Video module: https://github.com/bitfocus/companion-module-rode-rcv
- Bitfocus about / user numbers: https://bitfocus.io/about , https://bitfocus.io/companion
- Project health: https://insights.linuxfoundation.org/project/bitfocus-companion
