# County Codes

This file is a Weather Alert System reference for configuring `groups[].county_codes`.

SkyWarnPlus includes a `CountyCodes.md` file that lists U.S. county-style NWS alert zone codes, such as `NCC147` for Pitt County, North Carolina. Weather Alert System uses the same style of values in `config.yaml`.

## How to use county codes

Example:

```yaml
groups:
  - name: Farmville
    enabled: true
    county_codes: [NCC079, NCC147, NCC191, NCC195]
    talkgroup: 28502
```

The app will poll each listed code and announce matching active alerts on that group's talkgroup.

## Important county-code guidance

Prefer county-style codes such as:

```text
NCC147
NJC021
MDC033
```

Avoid public forecast zone codes such as:

```text
NCZ029
NJZ015
MDZ013
```

unless you know exactly why you need them. County-style alert requests include both county-based alerts and zone-based alerts associated with that county. Zone-only requests can miss county-based products.

## Upstream SkyWarnPlus source

The original SkyWarnPlus county-code reference is:

```text
https://github.com/Mason10198/SkywarnPlus/blob/main/CountyCodes.md
```

A helper script is included to refresh a full upstream copy when the system has internet access:

```bash
cd /opt/WeatherAlertSystem
sudo ./scripts/update_county_codes.sh
```

That script writes the downloaded upstream file to:

```text
docs/CountyCodes.upstream.md
```

## Common North Carolina examples

```text
NCC013  Beaufort County, NC
NCC049  Craven County, NC
NCC055  Dare County, NC
NCC061  Duplin County, NC
NCC065  Edgecombe County, NC
NCC069  Franklin County, NC
NCC079  Greene County, NC
NCC083  Halifax County, NC
NCC095  Hyde County, NC
NCC107  Lenoir County, NC
NCC127  Nash County, NC
NCC133  Onslow County, NC
NCC147  Pitt County, NC
NCC177  Tyrrell County, NC
NCC187  Washington County, NC
NCC191  Wayne County, NC
NCC195  Wilson County, NC
```

## Common test example

```text
NJC021  Mercer County, NJ / Trenton area
```
