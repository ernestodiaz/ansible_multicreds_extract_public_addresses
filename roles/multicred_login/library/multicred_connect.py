#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
Custom Ansible module: multicred_connect

Logs in to a network device (Cisco IOS/IOS-XE/NX-OS/ASA/XR, etc.) using a
list of candidate credentials and a list of candidate protocols (ssh,
telnet). It stops at the first successful combination. Once connected, it
pulls the running configuration, parses every interface's configured IPv4
address + mask, and returns ONLY the public (globally routable) addresses
together with the interface they live on and their calculated subnet ID.

This module intentionally does NOT use Ansible's network connection
plugins (network_cli) because those require the credential/protocol to
be known *before* the play starts (set on the host/connection vars).
Here we need to try combinations at runtime, per host, so we drive the
connection directly with Netmiko from inside the module.
"""

from ansible.module_utils.basic import AnsibleModule
import ipaddress
import re
import traceback

NETMIKO_IMPORT_ERROR = None
try:
    from netmiko import (
        ConnectHandler,
        NetmikoAuthenticationException,
        NetmikoTimeoutException,
    )
    HAS_NETMIKO = True
except Exception:
    HAS_NETMIKO = False
    NETMIKO_IMPORT_ERROR = traceback.format_exc()

try:
    from paramiko.ssh_exception import SSHException
    HAS_PARAMIKO = True
except Exception:
    HAS_PARAMIKO = False
    SSHException = Exception


# Map our protocol keyword + device_type "family" to the concrete
# Netmiko device_type string. Netmiko uses separate device_type values
# for telnet (suffix _telnet) vs ssh (base name).
DEVICE_TYPE_MAP = {
    ("cisco_ios", "ssh"): "cisco_ios",
    ("cisco_ios", "telnet"): "cisco_ios_telnet",
    ("cisco_xe", "ssh"): "cisco_xe",
    ("cisco_xe", "telnet"): "cisco_ios_telnet",
    ("cisco_nxos", "ssh"): "cisco_nxos",
    ("cisco_nxos", "telnet"): "cisco_nxos_telnet",
    ("cisco_asa", "ssh"): "cisco_asa",
    ("cisco_asa", "telnet"): "cisco_asa_telnet",
    ("cisco_xr", "ssh"): "cisco_xr",
    ("cisco_xr", "telnet"): "cisco_xr_telnet",
}


def build_device_type(base_type, protocol):
    """Resolve the Netmiko device_type for a given base family + protocol."""
    key = (base_type, protocol)
    if key in DEVICE_TYPE_MAP:
        return DEVICE_TYPE_MAP[key]
    # Fallback: generic suffix rule for any other cisco_* / generic types
    if protocol == "telnet":
        if base_type.endswith("_telnet"):
            return base_type
        return "{0}_telnet".format(base_type)
    return base_type


def flatten(text):
    """Collapse a multi-line exception message into a single, CSV/log
    friendly line (Netmiko exceptions often span many lines of
    troubleshooting hints we don't need verbatim in a report)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines)


# Matches a configured IPv4 address inside an interface block. Handles both
# the dotted-mask form used by IOS/IOS-XE/ASA/XR:
#     ip address 203.0.113.1 255.255.255.0
#     ipv4 address 203.0.113.1 255.255.255.0   (XR)
# and the CIDR form used by NX-OS:
#     ip address 203.0.113.1/24
# The optional trailing "secondary" keyword is tolerated and ignored.
_IP_DOTTED_RE = re.compile(
    r"^(?:ip|ipv4)\s+address\s+"
    r"(\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"(\d{1,3}(?:\.\d{1,3}){3})\b"
)
_IP_CIDR_RE = re.compile(
    r"^(?:ip|ipv4)\s+address\s+"
    r"(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})\b"
)
_INTERFACE_RE = re.compile(r"^interface\s+(\S+)")


def parse_interface_addresses(config_text):
    """
    Parse a running-config into a list of configured interface addresses.

    Returns a list of dicts:
        {"interface": str, "ip": str, "netmask": str, "prefixlen": int}

    Cisco configs indent interface sub-commands beneath a top-level
    ``interface <name>`` line and terminate each block with a non-indented
    line (typically ``!``). We track the current interface from the
    indentation, so an ``ip address`` line is always attributed to the
    interface it belongs to.
    """
    results = []
    current_if = None

    for raw_line in config_text.splitlines():
        if not raw_line.strip():
            continue

        indented = raw_line[0] in (" ", "\t")
        line = raw_line.strip()

        if not indented:
            # A top-level line ends any interface block. It might itself be
            # the start of a new interface block.
            match = _INTERFACE_RE.match(line)
            current_if = match.group(1) if match else None
            continue

        if current_if is None:
            continue

        cidr = _IP_CIDR_RE.match(line)
        if cidr:
            ip_str, prefix = cidr.group(1), int(cidr.group(2))
            try:
                network = ipaddress.ip_network(
                    u"{0}/{1}".format(ip_str, prefix), strict=False
                )
            except ValueError:
                continue
            results.append({
                "interface": current_if,
                "ip": ip_str,
                "netmask": str(network.netmask),
                "prefixlen": network.prefixlen,
            })
            continue

        dotted = _IP_DOTTED_RE.match(line)
        if dotted:
            ip_str, mask_str = dotted.group(1), dotted.group(2)
            try:
                network = ipaddress.ip_network(
                    u"{0}/{1}".format(ip_str, mask_str), strict=False
                )
            except ValueError:
                continue
            results.append({
                "interface": current_if,
                "ip": ip_str,
                "netmask": mask_str,
                "prefixlen": network.prefixlen,
            })

    return results


def extract_public_addresses(config_text):
    """
    From a running-config, return only the interface addresses that are
    public (globally routable) IPv4 addresses, each annotated with the
    calculated subnet ID (network address).

    Private (RFC1918), loopback, link-local, CGNAT (100.64/10), multicast,
    and other reserved/non-global addresses are skipped.
    """
    public = []
    for entry in parse_interface_addresses(config_text):
        try:
            addr = ipaddress.ip_address(u"{0}".format(entry["ip"]))
        except ValueError:
            continue

        if not addr.is_global:
            continue

        network = ipaddress.ip_network(
            u"{0}/{1}".format(entry["ip"], entry["prefixlen"]), strict=False
        )
        public.append({
            "interface": entry["interface"],
            "public_ip": entry["ip"],
            "netmask": entry["netmask"],
            "prefixlen": entry["prefixlen"],
            "subnet_id": str(network.network_address),
        })

    return public


def try_connect(host, port, base_device_type, protocol, username, password,
                 secret, timeout, command):
    """
    Attempt a single connection with one credential/protocol combo and,
    on success, capture the output of ``command`` (the config-gathering
    command).

    Returns a dict describing the outcome:
        {
          "ok": bool,
          "error": str or None,
          "output": str or None,
        }
    """
    device_type = build_device_type(base_device_type, protocol)

    device_params = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "secret": secret if secret else password,
        "timeout": timeout,
        "session_timeout": timeout,
        "auth_timeout": timeout,
        "banner_timeout": timeout,
        "fast_cli": False,
    }

    if port:
        device_params["port"] = port

    conn = None
    try:
        conn = ConnectHandler(**device_params)
        # Reading the running-config requires privileged mode on most
        # platforms, so enter enable if we are not already privileged.
        try:
            if not conn.check_enable_mode():
                conn.enable()
        except Exception:
            # Not fatal: some devices/users are already privileged or
            # enable isn't applicable (e.g. read-only views). We still
            # consider the login itself successful and try the command.
            pass

        output = None
        if command:
            # Config dumps can be long; give the read generous room.
            output = conn.send_command(command, read_timeout=max(timeout, 60))

        return {"ok": True, "error": None, "output": output}

    except NetmikoAuthenticationException as exc:
        return {"ok": False, "error": "auth_failed: " + flatten(str(exc)), "output": None}
    except NetmikoTimeoutException as exc:
        return {"ok": False, "error": "timeout: " + flatten(str(exc)), "output": None}
    except (SSHException,) as exc:
        return {"ok": False, "error": "ssh_error: " + flatten(str(exc)), "output": None}
    except Exception as exc:
        return {"ok": False, "error": "error: " + flatten(str(exc)), "output": None}
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def run_module():
    module_args = dict(
        host=dict(type="str", required=True),
        port=dict(type="int", required=False, default=None),
        device_type=dict(type="str", required=False, default="cisco_ios"),
        protocols=dict(type="list", elements="str", required=False, default=["ssh", "telnet"]),
        credentials=dict(
            type="list",
            elements="dict",
            required=True,
            options=dict(
                label=dict(type="str", required=False, default=None),
                username=dict(type="str", required=True),
                password=dict(type="str", required=True, no_log=True),
                secret=dict(type="str", required=False, default=None, no_log=True),
            ),
        ),
        # Command used to retrieve the interface IP configuration. The
        # default returns dotted-mask (IOS/XE/ASA/XR) or CIDR (NX-OS)
        # ``ip address`` lines, both of which the parser understands.
        interface_command=dict(type="str", required=False, default="show running-config"),
        timeout=dict(type="int", required=False, default=15),
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=False,
    )

    if not HAS_NETMIKO:
        module.fail_json(
            msg="The netmiko python package is required on the Ansible "
                "controller for this module. Install it with: "
                "pip install netmiko",
            error=NETMIKO_IMPORT_ERROR,
        )

    host = module.params["host"]
    port = module.params["port"]
    base_device_type = module.params["device_type"]
    protocols = module.params["protocols"]
    credentials = module.params["credentials"]
    interface_command = module.params["interface_command"]
    timeout = module.params["timeout"]

    if not credentials:
        module.fail_json(msg="credentials list is empty for host {0}".format(host))

    if not protocols:
        protocols = ["ssh"]

    attempts_log = []
    result = {
        "host": host,
        "status": "fail",
        "protocol_used": None,
        "credential_used": None,
        "public_ips": [],
        "attempts": 0,
        "last_error": None,
        "changed": False,
    }

    # Outer loop: credentials, in the order supplied (first in list = first tried)
    # Inner loop: protocols, in the order supplied (e.g. try ssh, then telnet)
    for cred in credentials:
        username = cred.get("username")
        password = cred.get("password")
        secret = cred.get("secret")
        cred_label = cred.get("label") or username

        for protocol in protocols:
            result["attempts"] += 1
            outcome = try_connect(
                host=host,
                port=port,
                base_device_type=base_device_type,
                protocol=protocol,
                username=username,
                password=password,
                secret=secret,
                timeout=timeout,
                command=interface_command,
            )

            attempts_log.append({
                "credential": cred_label,
                "protocol": protocol,
                "ok": outcome["ok"],
                "error": outcome["error"],
            })

            if outcome["ok"]:
                result["status"] = "success"
                result["protocol_used"] = protocol
                result["credential_used"] = cred_label
                result["last_error"] = None
                result["public_ips"] = extract_public_addresses(outcome["output"] or "")
                module.exit_json(
                    msg="Login succeeded on {0} using credential '{1}' over {2}; "
                        "found {3} public IP(s)".format(
                            host, cred_label, protocol, len(result["public_ips"])
                        ),
                    **result
                )
            else:
                result["last_error"] = outcome["error"]
                # fall through and try next protocol / credential

    # If we get here, every credential/protocol combination failed.
    result["status"] = "fail"
    module.exit_json(
        msg="All {0} credential/protocol combinations failed for host {1}".format(
            result["attempts"], host
        ),
        **result
    )


def main():
    run_module()


if __name__ == "__main__":
    main()
