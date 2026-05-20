# Managed OpenBridge mode

`managed_openbridge` is the default mode for the simplified configuration.

The app handles these local functions:

```text
NWS alert polling
  -> full spoken WAV
  -> USRP PCM UDP
  -> managed Analog_Bridge
  -> managed md380-emu
  -> managed MMDVM_Bridge
  -> embedded Homebrew/MMDVM receiver
  -> embedded OpenBridge UDP sender
  -> remote OpenBridge listener
```

You do not define per-group ports. Set one private internal port range:

```yaml
internal_ports:
  start: 43000
  step: 10
```

Each enabled group receives an isolated local helper chain from that range. All groups share the same remote OpenBridge endpoint.

## Minimal group config

```yaml
groups:
  - name: ARC125
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515
```

## OpenBridge self-test

Use this before a live transmission:

```bash
weather-alert-system -c config.yaml self-test-openbridge
```

The test starts a local UDP listener, simulates the Homebrew/MMDVM login that MMDVM_Bridge performs, injects synthetic `DMRD` voice frames for every enabled group, and verifies that outbound OpenBridge packets are produced with:

- 73-byte OpenBridge packet length
- `DMRD` payload
- configured OpenBridge network ID
- configured talkgroup
- valid HMAC-SHA1 using the configured passphrase

This verifies the embedded OpenBridge egress code without sending packets to the production remote listener.
