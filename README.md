# Multi-Credential / Multi-Protocol Cisco Login Auditor (Ansible)

Runs a command against a list of Cisco routers/switches when **there is no
single shared username/password** and devices may speak **SSH or Telnet**.

For each device it walks an ordered list of credentials. For each credential
it tries each protocol in order. It **stops at the first combination that
works**, then writes a CSV report with the status of every device.

## What you get in the CSV

`output/login_results.csv` columns:

| column | meaning |
|---|---|
| `device_name` | inventory hostname |
| `ip_address` | resolved `ansible_host` |
| `status` | `success` or `fail` |
| `credential_used` | label of the credential that worked (blank if all failed) |
| `protocol_used` | `ssh` or `telnet` (blank if all failed) |
| `attempts` | how many combinations were tried |
| `last_error` | single-line reason for the last failure (blank on success) |
| `timestamp` | UTC time the device was tested |

## How the cycling works

```
for credential in credential_list:        # order = priority, first tried first
    for protocol in login_protocols:       # e.g. ssh, then telnet
        try to connect + (optionally) run the command
        if it works -> record success, STOP this device
record fail only if every combination was exhausted
```

So credential #1 over SSH is tried first; if that fails, credential #1 over
Telnet; then credential #2 over SSH; and so on.

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
# Default command is "show version"
uv run ansible-playbook -i inventory/hosts.yml site.yml --ask-vault-pass

# Run a different command, or just test login with no command:
uv run ansible-playbook -i inventory/hosts.yml site.yml --ask-vault-pass \
    -e remote_command="show ip interface brief"

uv run ansible-playbook -i inventory/hosts.yml site.yml --ask-vault-pass \
    -e remote_command=""        # login-test only, no command run
```

The CSV lands at `output/login_results.csv`.

## Tuning

In `group_vars/all.yml`:
- `login_protocols` — global protocol order (default `ssh`, then `telnet`).
- `remote_command` — command to run once logged in.
- `login_timeout` — per-attempt timeout in seconds (default 15).

At the command line:
- `-e batch_size=N` — how many devices to attempt in parallel (default 20).

## Security notes

- Passwords live only in `files/secrets.yml`, which should be vault-encrypted.
  They are declared `no_log` in the module, so they are not printed even with
  `-vvv`. (Verified: a `-vvv` run shows zero plaintext password occurrences.)
- The CSV records the credential **label**, never the password.
- `output/login_results.csv` is written `0640`.

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
