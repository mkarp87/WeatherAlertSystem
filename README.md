# Weather Alert System

Weather Alert System polls the National Weather Service active-alert API, creates a spoken WAV for each alert group, feeds the WAV into a managed local DVSwitch helper chain, and sends outbound OpenBridge UDP traffic to one remote listener.

The default managed OpenBridge path is:

```text
Weather Alert System Python app
  -> full weather alert WAV
  -> USRP PCM UDP
  -> managed Analog_Bridge helper
  -> managed md380-emu helper
  -> managed MMDVM_Bridge helper
  -> embedded Homebrew/MMDVM receiver
  -> embedded OpenBridge sender
  -> remote OpenBridge UDP listener
```

No Asterisk and no local HBLink `bridge.py` process are required for the default mode.


## VoiceRSS / SkyDescribe audio

For higher-quality spoken alerts, use the SkyDescribe-compatible VoiceRSS TTS path. VoiceRSS is used only to create the WAV file; Analog_Bridge and md380-emu still perform the DMR/AMBE encoding step.  https://www.voicerss.org/

```yaml
SkyDescribe:
  APIKey: "CHANGE_ME_VOICERSS_API_KEY"
  Language: en-us
  Speed: 0
  Voice: John
  MaxWords: 300

tts:
  backend: voice_rss
```

The app sends VoiceRSS `hl`, `v`, `r`, `c=WAV`, and `f=8khz_16bit_mono`, then streams the resulting WAV into the managed bridge chain. Use `tts.backend: espeak` if the system must operate without a VoiceRSS account.

## Install

On a Debian/Ubuntu-style system:

```bash
cd /opt
git clone https://github.com/mkarp87/WeatherAlertSystem.git
cd WeatherAlertSystem
sudo ./install.sh
```

If the DVSwitch apt repository is not already configured, use the best-effort repository option:

```bash
sudo ./install.sh --add-dvswitch-repo
```

The installer creates a Python virtual environment, installs the Python app, installs system packages such as `espeak-ng` and `ffmpeg`, attempts to install the DVSwitch helper packages, creates `/opt/WeatherAlertSystem/config.yaml` if missing, and installs a systemd service.

After editing `config.yaml`:

```bash
/opt/WeatherAlertSystem/.venv/bin/weather-alert-system -c /opt/WeatherAlertSystem/config.yaml doctor
sudo systemctl enable --now weather-alert-system
```

## Simple config

Groups only need `name`, `enabled`, `county_codes`, and `talkgroup`.

```yaml
app:
  poll_interval_seconds: 60
  user_agent: "WeatherAlertSystem/1.0.1 info@example.org"
  dry_run: false

nws:
  included_events: []
  excluded_events: ["Test Message"]

openbridge:
  target_ip: "127.0.0.1"
  target_port: 54097
  passphrase: "SECRET"
  network_id: 12345

station:
  callsign: N0CALL"
  source_id: 1234567
  repeater_id: 12345
  slot: 1
  color_code: 1

internal_ports:
  start: 43000
  step: 10

groups:
  - name: ARC125
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515

  - name: ARC119
    enabled: false
    county_codes: [ARC119]
    talkgroup: 119
```

`internal_ports.start` is the beginning of the private local UDP port block. The app allocates all Analog_Bridge, MMDVM_Bridge, md380-emu, and embedded Homebrew/MMDVM ports inside that range. You do not need to define per-group ports.

## Commands

Validate config:

```bash
weather-alert-system -c config.yaml check-config
weather-alert-system -c config.yaml doctor
```

Run a safe local OpenBridge egress self-test. This does not contact the real remote OpenBridge server; it starts a local UDP listener, simulates MMDVM_Bridge DMRD voice frames, and verifies that OpenBridge packets leave the embedded sender with the configured network ID, talkgroup, and HMAC:

```bash
weather-alert-system -c config.yaml self-test-openbridge
```

Render generated helper configs without transmitting:

```bash
weather-alert-system -c config.yaml render-managed
```

Generate and transmit a test announcement:

```bash
weather-alert-system -c config.yaml test-audio --group ARC125 --text "Skywarn test announcement." --transmit --keep-bridges-seconds 20
```

Check helper logs:

```bash
tail -n 100 state/bridges/ARC125/logs/*.log
```

## Notes

`helpers` defaults to `auto`, which searches common binary locations and `$PATH` for `Analog_Bridge`, `MMDVM_Bridge`, and `md380-emu`. Set explicit paths only if `doctor` cannot find them.

The remote OpenBridge listener remains external. All enabled groups share the same OpenBridge target; group isolation is handled locally by one managed helper chain per enabled group.


## Persistent helper chains

In `managed_openbridge` mode, `weather-alert-system run` starts one Analog_Bridge, one MMDVM_Bridge, one md380-emu, and one embedded HBP/OpenBridge forwarder for each enabled group. These helper chains stay running for the lifetime of the service and are reused for every alert.

Do not run direct `test-audio --transmit` while the systemd service is already running unless you intentionally want a temporary second helper chain. Use the queue instead:

```bash
/opt/WeatherAlertSystem/.venv/bin/weather-alert-system \
  -c /opt/WeatherAlertSystem/config.yaml \
  queue-audio \
  --group ARC125 \
  --text "Skywarn test announcement."
```

The running service will synthesize the WAV and transmit it through the already-open helper chain.

## Internal port allocation

`internal_ports.start`, optional `internal_ports.end`, and `internal_ports.step` define the local-only UDP range used between the managed helper processes. At runtime the app probes the range and skips busy local UDP blocks, so an old helper process or unrelated service should not cause MMDVM_Bridge to fail with `Cannot bind the UDP address, err: 98`.


### Verify that v1.0 is installed

After unzipping and running `./install.sh --disable-helper-services`, verify the active code tree:

```bash
/opt/WeatherAlertSystem/.venv/bin/weather-alert-system version
grep -R "md380_emu_wrapper\|qemu-arm-static\|workdir" -n /opt/WeatherAlertSystem/skyalert_bridge
```

The grep must show `config.py`, `managed_openbridge.py`, and `helpers.py`. If it prints nothing, the archive was unpacked into the wrong directory or the service is still running older code.

## County code reference

Weather Alert System uses the same county-style NWS alert zone code format documented by SkyWarnPlus. Configure those values in each group's `county_codes` list:

```yaml
groups:
  - name: Farmville
    enabled: true
    county_codes: [NCC079, NCC147, NCC191, NCC195]
    talkgroup: 12345
```

Bundled references:

```text
docs/CountyCodes.md
docs/CountyCodes.upstream.md  # created by scripts/update_county_codes.sh
```

To refresh the full upstream SkyWarnPlus county-code file on a connected system:

```bash
cd /opt/WeatherAlertSystem
sudo ./scripts/update_county_codes.sh
```

Prefer county-style codes such as `NCC147` instead of forecast-zone codes such as `NCZ029`, unless you know why a zone-only query is needed.

## NWS event-name reference

The app filters alerts using the NWS alert `event` string. Put exact event names under `nws.included_events` to make an allow-list, or leave it empty to allow all events except `excluded_events`.

```yaml
nws:
  included_events:
    - "Tornado Warning"
    - "Severe Thunderstorm Warning"
    - "Flash Flood Warning"
  excluded_events:
    - "Test Message"
```

The full SkyWarnPlus-compatible list of 128 NWS v1.2 event names is bundled in:

```text
docs/EventTypes.md
```
