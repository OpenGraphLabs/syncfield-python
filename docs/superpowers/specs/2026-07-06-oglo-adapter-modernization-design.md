# Modernize the OGLO Tactile Adapter to Firmware v5 — Design

Date: 2026-07-06
Status: Approved (Jerry, 2026-07-06)
Repo: `syncfield-python` (SDK only — orchestrator and desktop app untouched)

Goal: replace the legacy OGLO tactile BLE adapter (a 5-FSR, IMU-less protocol
ported from an old egonaut build) with an implementation that matches the
**latest OGLO firmware** (`FW_REV 0.7.1-cfgfit`, `CONFIG_SCHEMA_VER 5`,
`packed12_v5`), using the `syncfield-swift` SDK as the behavioral reference.
Remove the legacy code entirely.

## Why

The shipped `OgloTactileStream` parses a `<HI` header + `<5H` per-sample
(one FSR per finger, u16, no IMU, no manifest) — a protocol the current
firmware no longer emits. The firmware now streams **80 taxels (5×4×4) at
12-bit** plus a **per-sample 6-axis raw IMU** and a device-clock timestamp,
described by a JSON manifest on the config characteristic. The Swift SDK
already consumes exactly this (`schema_ver 5`, `packed12_v5`). The Python
adapter must match so the glove is usable again.

## Reference-confirmed protocol (firmware source of truth)

`oglo_rdr02_ble.ino` + `BLE_PACKET_FORMAT_v5.md`; cross-checked against the
Swift `TactilePacketParser`.

- **GATT** (base `4652535F-424C-4500-…` = ASCII `FRS_BLE\0`): service `…0000`,
  notify `…0001`, config(READ) `…0002`, command `…0003` (reserved), log
  `…0004`, battery `…0005`.
- **Notify packet (packed12 v5, little-endian)**: 10-byte header —
  `count:u8`, `flags:u8` (bit 0x04 = packed12), `seq_base:u32`, `t_base_us:u32`
  — then `count` × 134-byte slots: `dt_us:u16`, 80 taxels packed 12-bit
  (120 B, 2 taxels / 3 bytes), 6× `i16` IMU (`ax,ay,az,gx,gy,gz`, raw LSB).
- **Manifest (config char, schema 5)** — actually-emitted keys: `device`,
  `schema_ver` (must be 5), `serial`, `side` (`left`/`right`), `hw_rev`,
  `fw_rev`, `rate_hz` (100), `samples_per_packet`, `adc_bits` (12),
  `stream_mode`, `values_per_sample` (80), `sample_order` (`finger,row,col`),
  `sample_shape` (`[5,4,4]`), `channels` (5 side-aware finger names),
  `device_id`, `pair_id`, `batch`, `factory_passed`, `cal_valid`, `imu`.
  The aspirational `packet_format`/`taxel_bits`/… keys in the v5 *doc* are NOT
  emitted; **detect the wire format from the notify flags byte, not the JSON.**
- **Timestamps**: device-provided per sample — `device_ns = (t_base_us +
  dt_us) * 1000`. `seq_base` gaps across packets = dropped packets. `t_base_us`
  is raw ESP32 `micros()` (wraps ~71 min).
- **No command writes**: the device streams at its firmware default the moment
  a central subscribes to the notify CCCD. The Swift SDK and firmware both note
  that command writes destabilize the BLE link, so we never write.
- **Discovery/identity**: advertised name is `OGLO` / `OGLO LEFT` / `OGLO
  RIGHT`; authoritative side comes from the manifest post-connect.

## Approved decisions (Jerry, 2026-07-06)

1. **IMU → separate derived substream** (parent_stream_id), mirroring Swift's
   `WristImuStream` and this SDK's OAK composite substream surface.
2. **No runtime command writes** — consume the firmware default stream; read
   the manifest for rate/side/labels/shape.
3. **packed12 v5 only, fail-loud** — validate `schema_ver == 5` and the notify
   `flags & 0x04`; raise a clear error on anything else. Legacy Method C/B
   (flags 0x02 / 0x01) is not supported.

## Architecture

New package `src/syncfield/adapters/oglo/`, mirroring Swift's separation so
pure logic is unit-testable without BLE/asyncio:

- **`packet.py`** — pure parser: `parse_v5(payload)` → `OgloPacket` of
  `OgloSample`s (`seq`, `device_us`, `taxels: list[int]`, `imu: tuple|None`);
  `unpack_taxels12`. No `bleak`/`asyncio` imports. Fail-loud on bad flags.
- **`manifest.py`** — `OgloDeviceManifest.from_json(bytes)` with
  `schema_ver == 5` validation (raises `OgloProtocolError` otherwise); exposes
  `side`, `rate_hz`, `values_per_sample`, `sample_shape`, `finger_labels`,
  and a `channel_label(taxel_index)` helper (`<finger>_<row>_<col>` using the
  side-aware finger order + `sample_shape`).
- **`stream.py`** — `OgloTactileStream(StreamBase)`: BLE lifecycle
  (connect → read+validate manifest → subscribe notify, **no writes**),
  emits 80-taxel `SampleEvent`s (channels `<finger>_<row>_<col>`, raw 0–4095,
  `device_ns`) for the primary sensor stream, and a derived **wrist-IMU
  substream** self-written to `{id}.imu.jsonl`.
- **`__init__.py`** — re-exports `OgloTactileStream`, `OgloDeviceManifest`,
  `OgloProtocolError`.

## IMU substream persistence

The primary taxel stream stays `produces_file=False`, so the orchestrator's
`SensorWriter` writes `{id}.jsonl`. The wrist IMU is a derived substream the
adapter **self-writes** to `{id}.imu.jsonl` via a reused `SensorWriter`,
following the OAK composite precedent exactly:

- The adapter gains an `_output_dir` attribute; the orchestrator's
  `_rebind_stream_output_dirs` sets it to the (rotated) episode dir each
  episode, so consecutive recordings stay idempotent (paths re-derived in
  `start_recording`, like OAK's `_rebind_output_paths`).
- It exposes the same duck-typed substream surface the desktop backend folds
  into `manifest.json`: `substreams()` → one `OgloSubstream("{id}.imu",
  "sensor", "Wrist IMU")` while recording, `recorded_artifacts()` → the
  `{id}.imu.jsonl` artifact after finalize, and `on_substream_sample()` /
  `_emit_substream_sample()` for live IMU preview.
- Headless-SDK note: the SDK orchestrator's `manifest.json` still lists only
  the primary stream (it doesn't fold substreams) — identical to OAK's
  headless behavior. The `{id}.imu.jsonl` file is always written to disk; the
  desktop layer adds the child manifest entry device-agnostically.

## Data model & output

- **Primary (`{id}.jsonl`)**: per taxel channels `<finger>_<row>_<col>` (raw
  12-bit 0–4095), `device_ns = (t_base_us + dt_us)*1000`, `capture_ns` = host
  arrival, `uncertainty_ns ≈ 0.5 ms`. Preview samples (pre-Record) emit with
  `frame_number = -1` and don't advance counters — unchanged from today.
- **Derived IMU (`{id}.imu.jsonl`)**: channels `ax,ay,az,gx,gy,gz` (raw i16 as
  int), same `device_ns`/`capture_ns`.
- **Hand**: `side` from the manifest is authoritative; the advertised-name
  hint (`OGLO LEFT/RIGHT`) only seeds the discovery listing.

## Error handling

- `schema_ver != 5` or missing `flags & 0x04` → `OgloProtocolError` (fail-loud),
  surfaced as an `ERROR` HealthEvent from the BLE session; the stream does not
  silently fall back to a legacy parse.
- Short header / truncated slot → `WARNING` HealthEvent, packet skipped.
- Taxels for every slot must be complete; a truncated trailing IMU yields
  `imu = None` for that sample (taxels preserved) — matches Swift.
- `seq_base` gap across packets → `DROP` HealthEvent.

## Discovery

Match advertised name substring `"oglo"` (case-insensitive) via the shared
`scan_peripherals` cache — the firmware always advertises an `OGLO…` name, so
this catches every real device. Hand hint parsed from `left`/`right` in the
name. Service-UUID matching is intentionally out of scope (the shared scanner
doesn't surface advertised service UUIDs, and name matching is sufficient).

## Testing

- **`test_packet.py`**: golden packed12 packet (N=3, 80 taxels) → correct
  `seq`/`device_us`/taxels/IMU; 12-bit unpack exactness (even/odd); truncated
  trailing IMU keeps taxels; non-0x04 flags raise; short header raises.
- **`test_manifest.py`**: schema-5 parse (side/rate/labels/shape), side-aware
  `channel_label`, `schema_ver != 5` raises, unknown keys ignored.
- **`test_oglo_tactile.py`** (rewrite): construction/capabilities, discovery
  (name filter, hand hint) with fake `bleak`, lifecycle decode via a sync test
  hook (taxel channels + device_ns, IMU substream split, recording anchor),
  fail-loud on bad manifest, lazy export, import guard.
- Remove the legacy `<HI>`/`<5H>` tests.

## Legacy removal

Delete `adapters/oglo_tactile.py`, its `<HI`+`<5H` parser, `FINGER_NAMES`-only
5-channel model, hardcoded `_SAMPLE_PERIOD_US`, and the old test module. Update
`adapters/__init__.py` (`_OPTIONAL` path → `oglo` package), the viewer's FSR
autoscale range (65535 → 4095), and the `examples/full_rig` OGLO references.

## Out of scope

Orchestrator substream-manifest support, service-UUID discovery, 9-axis
magnetometer (future firmware), runtime rate/format configuration, and any
desktop-app changes.
