# INTERCEPT UI/UX Improvements — Design Spec

**Date:** 2026-04-13  
**Status:** Approved  
**Scope:** Frontend (CSS, JS, HTML) + backend stubs for new features

---

## Overview

INTERCEPT has grown to 20+ modes faster than its design system has kept pace. The goal of this initiative is to establish a "Mission Control" visual identity — tactical, ops-center, every pixel earning its place — and apply it consistently across the entire application. Work is decomposed into four sequential sub-projects, each building on the last.

## Aesthetic Direction

**Mission Control.** Dense, tactical, ops-center feel. Radar aesthetics, glowing blips, HUD overlays, tight data-rich layouts. Think NORAD or air traffic control.

**Maps:** Premium dark map base (rich land/water separation, minimal labels) with tactical chrome on top — HUD corner panels, range rings, lat/lon grid, custom markers.

**Navigation:** Keep existing sidebar structure, polish execution — density, group headers, active states, collapsible groups.

---

## Sub-Project 1: Design System Uplift + Sidebar Polish

**Goal:** Establish the Mission Control visual language as the new baseline. Every subsequent sub-project inherits from this.

### CSS Token Changes (`static/css/core/variables.css`)

| Token | Current | New | Reason |
|---|---|---|---|
| `--bg-primary` | `#0a0c10` | `#06080d` | Deeper black for richer depth |
| `--bg-secondary` | `#0f1218` | `#090c12` | Consistent depth step |
| New: `--accent-cyan-glow` | — | `rgba(74,158,255,0.15)` | Panel borders and active states |
| `--text-xs` | `10px` | `9px` | Tighter data density |
| `--text-sm` | `12px` | `11px` | Tighter data density |
| New: `--scanline` | — | `repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px)` | Subtle scanline texture for visual containers |

### Sidebar (`templates/partials/nav.html`, `static/css/core/layout.css`)

- **Group headers:** Uppercase letter-spacing treatment with a fine left-border accent line.
- **Active mode button:** Replace background fill with a 3px left-border glow (`--accent-cyan` with blur) + subtle background tint.
- **Icon sizing:** Standardise all nav button icons to a consistent size (currently varies between modes).
- **Hover state:** Replace flat highlight with `--accent-cyan-glow` background.
- **Group collapse/expand:** Each nav group gets a toggle arrow. State persisted in `localStorage`. Allows users to hide groups they don't use.

### Global Panel Treatment (`static/css/core/components.css`)

- Panel headers: thin gradient top-border (`--accent-cyan` → transparent) for visual hierarchy.
- Panel indicator dots: pulse animation when active (extend existing `.panel-indicator.active` rule).
- Card backgrounds: subtle inner vignette via `box-shadow: inset 0 0 40px rgba(0,0,0,0.3)`.
- Scanline texture applied to all `.visuals-container` elements via `::after` pseudo-element.

---

## Sub-Project 2: Maps Overhaul

**Goal:** Premium map base + tactical chrome. Shared utility applied to all map-using modes.

**Affected pages:** ADS-B dashboard, AIS dashboard, Satellite dashboard, APRS, GPS, Radiosonde map.

### Tile Upgrade

- New default tile provider: **Stadia Maps "Alidade Smooth Dark"** — cleaner land/water separation, minimal label noise, free tier available (requires a free API key from stadiamaps.com; key stored in settings/env var `INTERCEPT_STADIA_KEY`).
- Add **"Tactical"** option in settings tile picker: near-black base, minimal labels, overlays do the visual work.
- Settings modal tile picker updated to include previews of each style.

### Custom Markers

Replace default Leaflet `divIcon` circles with SVG symbols:

- **Aircraft:** Rotated arrow/chevron by heading. Coloured by category: civil (`--accent-cyan`), military (`--accent-orange`), emergency (`--accent-red`). Sized by altitude band (high/mid/low).
- **Vessels:** Ship-silhouette SVG rotated by COG. Coloured by vessel type.
- **Satellites:** Simple diamond marker with pass arc overlay.
- **Selection popup:** Dark glass panel (backdrop-filter blur, `--bg-elevated` background, `--border-color` border), monospace data rows, no default Leaflet chrome.

### Tactical Overlays (shared map utility `static/js/map-utils.js`)

- **Range rings:** Dashed SVG circles at configurable NM/km intervals, label at ring edge.
- **Lat/lon grid:** Subtle graticule overlay, toggleable, 5° or 10° spacing.
- **Observer reticle:** Crosshair SVG replacing the current circle marker.
- **Flight/vessel trails:** Gradient fade — full opacity at current position, transparent at tail. Trail length configurable.
- **HUD corner panels:**
  - Top-left: contact count + mode name
  - Top-right: UTC clock + SDR status dot
  - Bottom-right: scale bar
  - Panels use `backdrop-filter: blur(8px)`, dark glass style.

### Implementation Note

Extract shared map initialisation into `static/js/map-utils.js`. Each dashboard calls `MapUtils.init(container, options)` and `MapUtils.addTacticalOverlays(map, options)`. Reduces duplication across the 6 map-using pages.

---

## Sub-Project 3: Mode Polish Sprint

**Goal:** Apply the new design system to the 12 flagged modes. Consistent structure, no mode feeling like a rough prototype.

**Flagged modes:** Pager, 433MHz Sensors, Sub-GHz, Morse/OOK, GPS, APRS, Radiosonde, WeFax, System Health, Meshtastic, VDL2, ACARS.

### Shared Treatment (all 12 modes)

- **Scan indicator:** Animated dot + status text in list header. Pattern from WiFi/Bluetooth applied universally.
- **Sort + filter controls row:** Where a list exists, add sort controls (signal/name/time) in the list header.
- **Empty states:** All modes use `components/empty_state.html` consistently with mode-specific copy.
- **Control panels:** Unified layout — label left, input right, consistent `--space-3` gaps.
- **Data rows:** 2-line row treatment — primary info on line 1, secondary metadata on line 2 (signal level, timestamp, etc.).

### Mode-Specific Highlights

**Pager, ACARS, VDL2:**
- Message rows: signal strength bar on the left edge, collapsible raw data section, relative timestamp with absolute on hover.
- Message type badge (coloured by category/priority).

**433MHz Sensors:**
- Sensor cards grouped by device type with a category header.
- Last-seen freshness indicator (green → amber → red based on age).
- Per-sensor RSSI sparkline (reuse Chart.js pattern from WiFi signal history).

**Sub-GHz, Morse/OOK, WeFax:**
- Signal visualisation panels get scanline texture + dark glass aesthetic.
- WeFax image gallery upgraded: lightbox overlay, larger thumbnails, metadata strip below each image.

**Meshtastic:**
- Chat-style message layout (sent right, received left, timestamp inline).
- Node list with signal quality bars (RSSI + SNR visualised as bar segments).

**System Health:**
- Metric cards in Grafana style: large value, small label, sparkline, status colour border.
- Process list: coloured status pill (running/stopped/error), uptime, PID.

**GPS, APRS, Radiosonde:**
- Map-centric layout using the Sub-Project 2 shared map utility (requires Sub-Project 2 complete first).
- Sub-Project 2 handles the map surface itself (tiles, markers, overlays). Sub-Project 3 handles the surrounding UI: control panel layout, telemetry sidebar panel, empty states, and scan indicator.
- These two modes should be done last in the Sub-Project 3 sprint, after Sub-Project 2 is merged.

---

## Sub-Project 4: New Features

### 4a — Spectrum Overview Dashboard (`/spectrum`)

A new full-screen dashboard showing a wideband frequency sweep (rtl_power) as a waterfall/bar chart. Colour-coded markers overlay the sweep at frequencies used by active modes. Clicking a marker navigates to that mode.

- Backend: new `/spectrum` route + SSE stream from rtl_power.
- Frontend: Chart.js frequency bar chart, mode markers as overlaid labels.
- Entry point: new nav item in Signals group, plus a link from the System Health mode.

### 4b — Alerts & Trigger Engine

Rule-based alert system evaluated server-side against SSE event streams.

- **Rule schema:** `{ event_type, condition_field, condition_op, condition_value, action_type, action_payload }`
- **Storage:** New `alerts` SQLite table in `instance/`.
- **UI:** Rule builder modal accessible from a new "Alerts" nav item. Rule list with enable/disable toggle, test button.
- **Initial event types:**
  - Aircraft: callsign match, squawk code, altitude threshold, military flag
  - Bluetooth: new tracker detected, specific MAC seen
  - Pager: message keyword match
  - AIS: vessel type, MMSI match
- **Actions:** Desktop notification, audio alert (reuse existing alert sounds), log to file.

### 4c — Signal Recording & Playback

Per-mode session recording of decoded output (not I/Q — storage/complexity deferred).

- **Record:** "Record" toggle button in each mode's control panel. Saves decoded events as timestamped JSON to `data/recordings/<mode>/<timestamp>.json`.
- **Playback:** Recording browser in a modal. Select a recording, choose playback speed (1×, 2×, 5×). Playback re-streams events through the existing SSE infrastructure via a `/playback/stream` endpoint.
- **I/Q recording:** Out of scope for this phase.

### 4d — Signal Identification

"What is this?" capability in the Listening Post (waterfall) mode.

- User selects a frequency range on the waterfall.
- Backend calls `inspectrum` (if installed) or a lightweight Python classifier against the captured I/Q segment.
- Returns: estimated modulation type, bandwidth, symbol rate, and a match against a curated known-signals database (JSON file).
- UI: result panel slides up from the bottom of the waterfall with signal details and suggested decoder.
- Graceful degradation: button is hidden if `inspectrum` is not installed.

### 4e — Mobile PWA

Installable, responsive INTERCEPT for field use.

- `static/manifest.json`: name, icons, display standalone, theme colour.
- Service worker (`static/sw.js`): cache shell assets for offline load. SSE streams remain live-only.
- **Responsive breakpoints** (`static/css/responsive.css`): sidebar collapses to bottom tab bar on `max-width: 768px`. Priority mobile views: BT Locate, WiFi Locate, GPS (field-use modes). Map-heavy modes scale gracefully. Control panels stack vertically on narrow screens.
- Home screen icons: generate from existing `favicon.svg` at 192×192 and 512×512.

---

## Implementation Order

1. Sub-Project 1 — Design System + Sidebar (foundation, ~1 week)
2. Sub-Project 2 — Maps Overhaul (high impact, ~1 week)
3. Sub-Project 3 — Mode Polish Sprint (breadth, ~2 weeks)
4. Sub-Project 4 — New Features (sequentially: Spectrum → Alerts → Recording → Signal ID → PWA)

## Out of Scope

- I/Q signal recording (deferred to a later phase)
- Backend infrastructure changes beyond what new features require
- New SDR hardware support
- Existing WiFi and Bluetooth modes (recently redesigned, not flagged)
