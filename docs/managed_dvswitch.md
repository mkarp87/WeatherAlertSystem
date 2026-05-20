# Managed DVSwitch mode

`output.mode=managed_dvswitch` lets Weather Alert System keep one service and one YAML file while still using the existing bridge binaries that already solve DMR AMBE and network details.

## Why per-group instances are needed

A single Analog_Bridge USRP side has one active transmit talkgroup at a time. If the app sends audio for TG 125 and changes the same bridge to TG 119 before TG 125 finishes, audio can cross. The fix is to isolate the active audio path for every group that may transmit concurrently.

Recommended shape:

```text
ARC125 audio -> USRP 34001 -> Analog_Bridge ARC125 -> md380-emu 2470 -> MMDVM_Bridge ARC125 -> HBLink/OpenBridge -> TG 125
ARC119 audio -> USRP 34011 -> Analog_Bridge ARC119 -> md380-emu 2471 -> MMDVM_Bridge ARC119 -> HBLink/OpenBridge -> TG 119
ARC201 audio -> USRP 34021 -> Analog_Bridge ARC201 -> md380-emu 2472 -> MMDVM_Bridge ARC201 -> HBLink/OpenBridge -> TG 201
```

Weather Alert System locks only the specific USRP endpoint it is using. Separate endpoints can run in parallel; shared endpoints remain serialized.

## What the app manages

For each `groups[].bridge` section, the app can:

1. Render files listed in `bridge.files`.
2. Start commands listed in `bridge.processes`.
3. Stream that group announcement to `bridge.usrp`.
4. Stop child processes when the app exits.

The app does not bundle or install DVSwitch, HBLink3, md380-emu, firmware, or hardware AMBE software. The child commands are ordinary local process commands and can be changed to match your system.
Commands without an explicit `cwd` run from the directory containing `config.yaml`, so relative paths in process commands and rendered file paths stay consistent.

## Placeholders

These placeholders are available in rendered files and process commands:

```text
{config_dir}
{state_dir}
{audio_dir}
{bridge_dir}
{analog_bridge_ini}
{dvswitch_ini}
{mmdvm_bridge_ini}
{hblink_cfg}
{rules_py}
{group}
{talkgroup}
{county_codes}
{usrp_address}
{usrp_tx_port}
{usrp_local_rx_port}
{usrp_sample_rate}
{usrp_frame_ms}
{slot}
{color_code}
{subscriber_id}
{repeater_id}
{callsign}
```

Every key in `bridge.variables` is also available.

## md380-emu

Analog_Bridge should point at the md380-emu server for the same isolated chain:

```ini
[GENERAL]
decoderFallBack = true
useEmulator = true
emulatorAddress = 127.0.0.1:2470
```

Then start the matching emulator process:

```yaml
processes:
  - name: md380-emu
    command: "/opt/md380-emu/md380-emu -S {md380emu_port}"
```

Use a unique `md380emu_port` for each simultaneously active bridge chain.

## OpenBridge choices

You can either:

1. Run one HBLink instance per group, each with its own `OPENBRIDGE` section and ACL restricted to that group's talkgroup; or
2. Run one shared HBLink/OpenBridge instance and point all per-group MMDVM_Bridge peers at it.

The first option is stricter and easier to reason about. The second option has fewer child processes but relies on the shared HBLink config to avoid unwanted routing.

`examples/managed_dvswitch.yaml` shows the stricter one-HBLink-per-group pattern.


## HBLink forwarding

For traffic to leave the local HBLink MASTER and go out OPENBRIDGE, use HBLink3 `bridge.py` with a rules file. Starting `hblink.py` alone is useful for base peer/master testing, but it does not apply conference-bridge routing rules. The managed example therefore renders both `hblink.cfg` and `rules.py` and starts:

```bash
python3 /opt/hblink3/bridge.py -c {hblink_cfg} -r {rules_py}
```

The generated `rules.py` joins `MASTER-{group}` and `OBP-{group}` on the configured talkgroup, so traffic from MMDVM_Bridge is forwarded to OpenBridge.

## Diagnostics

Use these commands first when the Python app says it streamed USRP audio but the remote side does not key up:

```bash
weather-alert-system -c config.yaml check-config
weather-alert-system -c config.yaml doctor
weather-alert-system -c config.yaml render-managed
weather-alert-system -c config.yaml test-audio --group ARC125 --text "Skywarn test announcement." --transmit --keep-bridges-seconds 15
tail -n 80 state/bridges/ARC125/logs/*.log state/bridges/ARC125/hblink.log
```
