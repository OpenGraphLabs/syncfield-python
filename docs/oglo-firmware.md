# OGLO capture and firmware

`OgloTactileStream` uses its own USB collector and does not call the OGLO Python
SDK updater. Install its current optional dependencies with
`python -m pip install "syncfield[ble]"` (this extra includes pyserial).

## Firmware 0.9.17 preparation

The signed 0.9.17 USB FIFO fix keeps the schema-6 wire format. Its current
scope, exact image hashes and known qualification gaps are in the
[firmware status](https://github.com/OpenGraphLabs/oglo-hardware/blob/main/docs/firmware-0.9.17-status.md).
It remains test firmware; successful SDK rc7 recordings do not qualify this
collector's own recording path or every glove.

Installing `oglo` does not add automatic firmware preparation to this collector.
Automatic preparation runs only through the OGLO SDK's `connect()` / `connect_pair()`
when explicitly enabled in that Python environment.

For a supervised USB update, finish the recording and stop every process using
the selected gloves, including background collectors and browser USB sessions.
Use a separate Python 3.10+ environment on a macOS or Linux host with USB access:

```sh
python3 -m venv .venv-oglo-update
. .venv-oglo-update/bin/activate
curl -fsSL https://github.com/OpenGraphLabs/oglo-python/releases/download/v0.1.0rc8.dev2/install.py | python -
python -m oglo firmware prepare --serial OGLO-L-00001 --serial OGLO-R-00001
```

Replace the example serials with the actual selected gloves. This explicit
`prepare` command may write firmware; the installation command alone does not.
Do not run it during capture. Compatibility checks, signed-image verification,
configuration/calibration preservation and post-boot checks are described in the
[SDK preparation guide](https://github.com/OpenGraphLabs/oglo-python/blob/main/docs/10_managed_firmware.md).
Unsupported hardware/images are refused. Do not use a merged recovery image as a
routine application update. BLE firmware updating is not supported.

If preparation fails, retain its error and local evidence and resolve the device
state before restarting collection. After success, return to the normal collector
and verify both hands, actual saved coverage, per-modality sequence gaps, device
drops, packet rates and host arrival gaps on the intended host. A packet rate is
not proof of a fresh sensor conversion rate or zero transport delay.

SDK installation records remain local. They do not activate hardware-ops rollout
tracks or automatically reconcile its unit installation ledger.
