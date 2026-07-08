# Multi-Credential / Multi-Protocol Cisco Public-IP Inventory (Ansible)

Logs in to a list of Cisco routers/switches when **there is no single shared
username/password** and devices may speak **SSH or Telnet**, then inventories
the **public IP addresses** configured on their interfaces.

For each device it walks an ordered list of credentials. For each credential
it tries each protocol in order. It **stops at the first combination that
works**, reads the running config, and extracts every interface's configured
IPv4 address — keeping only the **public (globally routable)** ones. Private
(RFC1918), loopback, link-local, CGNAT and other reserved addresses are
ignored. Devices with no public IP are left out of the report entirely.

## What you get in the CSV

`output/public_ip_inventory.csv` — **one row per public IP** (a device with
several public IPs produces several rows; a device with none is omitted):

| column | meaning |
|---|---|
| `device_name` | inventory hostname |
| `management_ip` | the device's management address (resolved `ansible_host`) |
| `public_ip` | a public IPv4 address configured on an interface |
| `subnet_mask` | that address's mask in dotted-decimal form |
| `interface` | the interface the public IP is configured on |
| `interface_description` | the interface's configured `description` (empty if none) |
| `interface_status` | `UP`, `Down`, or `Administratively Down` (see below) |
| `subnet_id` | the network/subnet ID in CIDR form (`network/prefix`), calculated from the IP + mask |

### Interface status

After reading the config, the module runs a platform-appropriate status
command on the same session — `show ip interface brief` (IOS/IOS-XE/NX-OS),
`show ipv4 interface brief` (XR), or `show interface ip brief` (ASA) — and
maps each interface to `UP` (line and protocol up), `Down`, or
`Administratively Down`. If the account is not allowed to run that command,
status falls back to the config itself: `shutdown` present →
`Administratively Down`, otherwise `Unknown` (UP vs Down can't be told from
config alone).

## Exclusions log

Every run also writes `output/log_<MMDDYYYY_HH>.log` (e.g.
`log_07082026_14.log` — month, day, year, hour of the run) listing the
devices that were **left out of the CSV** and why:

```
Run: 2026-07-08 14:03 — 1 login failure(s), 1 device(s) with no public IP
LOGIN_FAILED  old-rtr99 (10.10.9.9) — auth_failed: Authentication to device failed.
NO_PUBLIC_IP  lan-sw01 (10.10.2.1) — logged in OK, no public IPs found
```

`LOGIN_FAILED` = every credential/protocol combination failed (the last
error is included); `NO_PUBLIC_IP` = login worked but no public interface
IP was found. If nothing was excluded, the log says so.

## How the cycling works

```
for credential in credential_list:        # order = priority, first tried first
    for protocol in login_protocols:       # e.g. telnet, then ssh
        try to connect
        if it works -> pull running config, extract public IPs, STOP this device
record fail only if every combination was exhausted
```

So credential #1 over the first protocol is tried first; if that fails,
credential #1 over the next protocol; then credential #2; and so on. Only the
first working combination is used to read the config.

## How public IPs are identified

The running config is parsed for interface `ip address` lines in both the
dotted-mask form (`ip address 203.0.113.1 255.255.255.0`, used by
IOS/IOS-XE/ASA/XR) and the CIDR form (`ip address 203.0.113.1/24`, used by
NX-OS), including `secondary` addresses. Each address is classified with
Python's `ipaddress` module; only **globally routable** addresses are kept.
The interface's `description` and `shutdown` state are picked up from the
same interface block. The subnet ID is the network derived from the IP and
its mask, in CIDR form (e.g. `8.8.8.129 / 255.255.255.192` →
`8.8.8.128/26`).

## Layout

```
ansible-multicred/
├── site.yml                         # main playbook (run this)
├── pyproject.toml                   # python deps for the controller (uv)
├── group_vars/all.yml               # protocols, command, timeout defaults
├── inventory/
│   ├── hosts.yml                    # your devices (edit or generate from CSV)
│   └── devices.csv.template         # CSV template for csv_to_hosts.py
├── scripts/
│   └── csv_to_hosts.py             # generate hosts.yml from a CSV device list
├── files/
│   ├── credentials.yml              # ORDERED credential list (labels + refs)
│   └── secrets.yml                  # the actual passwords (ENCRYPT THIS)
└── roles/multicred_login/
    ├── tasks/main.yml               # per-device driver + result fragment
    └── library/multicred_connect.py # custom module doing the real work
```

## Setup

1. Install dependencies on the Ansible controller:
   ```bash
   uv sync
   ```

2. Populate `inventory/hosts.yml` — either edit it directly or generate it
   from a CSV (see [Generating the inventory from CSV](#generating-the-inventory-from-csv)).
   Each device needs `ansible_host` (IP or FQDN) and `device_platform`
   (`cisco_ios`, `cisco_xe`, `cisco_nxos`, `cisco_asa`, `cisco_xr`).
   The module auto-selects the right Netmiko driver for SSH vs Telnet.
   Optionally pin `device_port` or override `login_protocols` per host
   (e.g. a telnet-only legacy box).

3. Put the ordered credential list in `files/credentials.yml`. Each entry has
   a friendly `label` (what shows up in the CSV), a `username`, a `password`
   (referenced from the vault), and an optional `secret` (enable password).

4. Put the real passwords in `files/secrets.yml` and **encrypt it**:
   ```bash
   ansible-vault encrypt files/secrets.yml
   ```

## Generating the inventory from CSV

`scripts/csv_to_hosts.py` converts a CSV device list into `inventory/hosts.yml`
so you don't have to hand-edit YAML.

**CSV format** (see `inventory/devices.csv.template`):

```
name,ip address,device type
core-sw01,10.10.1.1,ios
dist-rtr01,10.10.2.1,ios-xe
nx-sw01,10.10.9.5,nxos
```

Lines starting with `#` are treated as comments and ignored.

**Accepted `device type` values:**

| Short alias | Full Netmiko driver |
|---|---|
| `ios` | `cisco_ios` |
| `ios-xe` / `xe` | `cisco_xe` |
| `nxos` | `cisco_nxos` |
| `asa` | `cisco_asa` |
| `ios-xr` / `xr` | `cisco_xr` |

Full driver names (`cisco_ios`, `cisco_xe`, …) are also accepted directly.

**Usage:**

```bash
# Generate inventory/hosts.yml from your CSV
python scripts/csv_to_hosts.py inventory/devices.csv

# Overwrite an existing hosts.yml
python scripts/csv_to_hosts.py inventory/devices.csv --force

# Preview the output without writing (dry run)
python scripts/csv_to_hosts.py inventory/devices.csv --dry-run

# Use a custom output path
python scripts/csv_to_hosts.py devices.csv -o /tmp/hosts.yml

# Allow unknown device types (passed through verbatim with a warning)
python scripts/csv_to_hosts.py inventory/devices.csv --allow-unknown
```

The script validates every row (hostname format, IP/FQDN syntax, duplicate
names, known device type) and prints clear errors before writing anything.

## Run

```bash
# Reads the running config with the default command ("show running-config")
uv run ansible-playbook -i inventory/hosts.yml site.yml --ask-vault-pass

# Use a scoped command instead (e.g. for a read-only account):
uv run ansible-playbook -i inventory/hosts.yml site.yml --ask-vault-pass \
    -e interface_command="show running-config interface"
```

The CSV lands at `output/public_ip_inventory.csv`; the exclusions log at
`output/log_<MMDDYYYY_HH>.log`.

## Tuning

In `group_vars/all.yml`:
- `login_protocols` — global protocol order (default `telnet`, then `ssh`).
- `interface_command` — command whose output is parsed for interface IPs.
- `login_timeout` — per-attempt timeout in seconds (default 15).

At the command line:
- `-e batch_size=N` — how many devices to attempt in parallel (default 20).

## Security notes

- Passwords live only in `files/secrets.yml`, which should be vault-encrypted.
  They are declared `no_log` in the module, so they are not printed even with
  `-vvv`. (Verified: a `-vvv` run shows zero plaintext password occurrences.)
- The CSV never contains any credential material — only device names,
  management IPs, and public interface addressing.
- `output/public_ip_inventory.csv` is written `0640`.

## Notes on design

The module talks to devices with **Netmiko** directly rather than Ansible's
`network_cli` connection plugin. That plugin needs the protocol and credential
fixed before the play runs, which can't express "try these in order until one
works." Driving Netmiko inside a custom module is what makes the runtime
cycling possible.

Results are aggregated by having each host write its own JSON fragment to a
local scratch dir, which a final `localhost` play merges into the CSV. This
avoids the well-known problem that `set_fact` does not reliably share facts
across hosts running in parallel forks.
