# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

This is a **Multi-Credential / Multi-Protocol Cisco Public-IP Inventory** tool built with Ansible. It logs in to a list of network devices when there is no single shared credential, cycling through an ordered list of credentials and protocols (Telnet then SSH by default) and stopping at the first working combination per device. Once logged in it reads the running config, extracts every interface's IPv4 address, keeps only the **public (globally routable)** ones, and writes them — with mask, interface, interface description, interface status (`UP` / `Down` / `Administratively Down`), and subnet ID in CIDR form — to `output/public_ip_inventory.csv`. Devices with no public IP are omitted from the report; each run also writes `output/log_<MMDDYYYY_HH>.log` listing the excluded devices (login failures and devices with no public IP).

## Dependencies

Install Python dependencies on the Ansible controller (using uv or pip):

```bash
uv sync
# or
pip install netmiko paramiko ansible-core
```

## Running the playbook

```bash
# Default: reads running config with "show running-config" on all devices
ansible-playbook -i inventory/hosts.yml site.yml --ask-vault-pass

# Use a scoped config command (e.g. for read-only accounts)
ansible-playbook -i inventory/hosts.yml site.yml --ask-vault-pass -e interface_command="show running-config interface"

# Control parallelism (default 20)
ansible-playbook -i inventory/hosts.yml site.yml --ask-vault-pass -e batch_size=5
```

## Vault management

```bash
# Encrypt secrets file (required before committing)
ansible-vault encrypt files/secrets.yml

# Edit an encrypted secrets file
ansible-vault edit files/secrets.yml
```

## Architecture

The project is flat (no subdirectory structure enforced at the repo root) with these key files:

| File | Role |
|---|---|
| `site.yml` | Main playbook — two plays: one running `multicred_login` role across `network_devices`, then a `localhost` play that merges JSON fragments into the CSV (one row per public IP) and writes the per-run exclusions log |
| `multicred_connect.py` | Custom Ansible module — drives Netmiko directly (bypasses `network_cli`) to attempt credential/protocol combinations at runtime, then parses the running config for public interface IPs |
| `hosts.yml` | Inventory — devices under `network_devices` group; per-host `device_platform` selects the Netmiko driver family |
| `all.yml` | Group vars — global defaults: `login_protocols`, `interface_command`, `login_timeout` |
| `credentials.yml` | Ordered credential list (labels + vault variable references); first entry is tried first |
| `secrets.yml` | Actual passwords (vault variables referenced by `credentials.yml`); **must be encrypted** |
| `main.yml` | Role tasks — calls the module, builds a result dict (incl. `public_ips`), writes a per-host JSON fragment to `output/.fragments/` |

### Key design decisions

**Why a custom module instead of `network_cli`?** Ansible's `network_cli` connection plugin requires the protocol and credential to be fixed before the play starts. The custom `multicred_connect` module drives Netmiko directly, enabling the runtime cycling loop.

**Why per-host JSON fragments?** `set_fact` values are not reliably shared across hosts running in parallel forks. Each host writes `output/.fragments/<hostname>.json` (containing `public_ips`); the final localhost play flattens them into the CSV, avoiding race conditions.

**Credential/protocol iteration order:** Outer loop = credentials (by list position), inner loop = protocols. So credential #1 over the first protocol is tried first, then credential #1 over the second, then credential #2, etc. Only the first working combination reads the config.

**Public-IP extraction:** After login the module parses the running config for interface `ip address` lines — both dotted-mask (`ip address 203.0.113.1 255.255.255.0`, IOS/XE/ASA/XR) and CIDR (`ip address 203.0.113.1/24`, NX-OS), including `secondary` addresses. The interface's `description` and `shutdown` state are captured from the same block. Python's `ipaddress` module classifies each; only `is_global` addresses are kept. The subnet ID is emitted in CIDR form: `network_address/prefixlen` from `ip_network(ip/mask, strict=False)`. Devices with zero public IPs are dropped from the CSV via `rejectattr('public_ips', 'equalto', [])` + `subelements`.

**Interface status:** After reading the config, the module runs a per-platform status command on the same session (`show ip interface brief` for IOS/XE/NX-OS, `show ipv4 interface brief` for XR, `show interface ip brief` for ASA; see `STATUS_COMMAND_MAP`) and parses it into `UP` / `Down` / `Administratively Down`. Interface names are matched between brief output and config via `canonical_ifname()` (first two letters of the de-hyphenated prefix + port numbering, so `Eth1/1` matches `Ethernet1/1`). If the status command fails (restricted account), the fallback is config-derived: `shutdown` → `Administratively Down`, otherwise `Unknown`.

**Exclusions log:** The final localhost play writes `output/log_<MMDDYYYY_HH>.log` (`strftime('%m%d%Y_%H')`) with a `LOGIN_FAILED` line per device whose every credential/protocol combination failed (including `last_error`) and a `NO_PUBLIC_IP` line per device that logged in but had no public IP.

**`device_platform` values** map to Netmiko driver families: `cisco_ios`, `cisco_xe`, `cisco_nxos`, `cisco_asa`, `cisco_xr`. The module appends `_telnet` automatically for Telnet connections.

### Per-host overrides

Override global vars on individual inventory hosts:
- `login_protocols` — restrict a device to `[telnet]` only
- `device_port` — non-standard SSH/Telnet port
