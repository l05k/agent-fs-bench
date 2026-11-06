"""Shared SSH host inventory and Open MPI remote-shell agent.

The multi-host benchmark and mount-scale read the same private TOML inventory.
Open MPI execs this module's absolute source path with the controller Python,
so the agent CLI below imports nothing from the package and needs no install.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import sys
import tomllib

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class Host:
    name: str
    address: str
    user: str | None = None
    port: int = 22
    ssh_args: tuple[str, ...] = ()
    python: str = "python3"


def load_hosts(path: Path) -> tuple[Host, ...]:
    try:
        with path.expanduser().open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ValueError(
            f"cannot read hosts file {path}: {exc}; copy "
            "experiments/hosts.example.toml to local/hosts.toml and edit the "
            "SSH host entries"
        ) from exc
    if set(document) != {"hosts"} or not isinstance(document["hosts"], list):
        raise ValueError("hosts TOML must contain a [[hosts]] list only")
    hosts: list[Host] = []
    names: set[str] = set()
    endpoints: set[tuple[str | None, str, int]] = set()
    for item in document["hosts"]:
        if not isinstance(item, dict) or set(item) - {"name", "address", "user", "port", "ssh_args", "python"}:
            raise ValueError("unknown host configuration keys")
        name, address = item.get("name"), item.get("address")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or name in names:
            raise ValueError("host names must be unique safe aliases")
        if not isinstance(address, str) or not address or address.startswith("-") or any(c.isspace() for c in address):
            raise ValueError(f"{name}: invalid SSH address")
        user = item.get("user")
        if user is not None and (not isinstance(user, str) or not _NAME_RE.fullmatch(user)):
            raise ValueError(f"{name}: invalid SSH user")
        port = item.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"{name}: port must be in 1..65535")
        ssh_args = item.get("ssh_args", [])
        if not isinstance(ssh_args, list) or any(not isinstance(s, str) or not s for s in ssh_args):
            raise ValueError(f"{name}: ssh_args must be an array of non-empty strings")
        python = item.get("python", "python3")
        if not isinstance(python, str) or not python or "\x00" in python:
            raise ValueError(f"{name}: invalid Python executable")
        endpoint = (user, address, port)
        if endpoint in endpoints:
            raise ValueError(f"{name}: duplicate SSH endpoint")
        endpoints.add(endpoint)
        hosts.append(Host(name, address, user, port, tuple(ssh_args), python))
        names.add(name)
    return tuple(hosts)


def ssh_argv(host: Host, connect_timeout_s: int = 15) -> list[str]:
    destination = f"{host.user}@{host.address}" if host.user else host.address
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o",
            f"ConnectTimeout={connect_timeout_s}", "-o", "LogLevel=ERROR",
            *host.ssh_args, "-p", str(host.port), "--", destination]


def openmpi_agent(hosts_path: Path, connect_timeout_s: int = 15) -> str:
    """Return the ``--mca plm_rsh_agent`` value for an Open MPI 4 controller.

    Open MPI splits the value on ``:`` into candidate agents and each candidate
    on spaces, without quote processing, so every token must be a literal
    whitespace- and colon-free word.
    """
    if type(connect_timeout_s) is not int or connect_timeout_s < 1:
        raise ValueError("connect_timeout_s must be a positive integer")
    if not sys.executable:
        raise ValueError("controller Python executable is unknown")
    tokens = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--hosts", str(hosts_path.expanduser().resolve()),
        "--connect-timeout", str(connect_timeout_s),
    ]
    for token in tokens:
        if not token or "\x00" in token or any(c.isspace() for c in token) or ":" in token:
            raise ValueError(
                "Open MPI splits plm_rsh_agent on spaces and colons; "
                f"cannot represent token {token!r}"
            )
    return " ".join(tokens)


def _parse_agent_args(argv: list[str]) -> tuple[Path, int, str | None, str, list[str]]:
    hosts_path: Path | None = None
    connect_timeout_s = 15
    user: str | None = None
    index = 0
    while index < len(argv):
        option = argv[index]
        if option == "--hosts":
            if hosts_path is not None:
                raise ValueError("--hosts specified more than once")
            if index + 1 >= len(argv):
                raise ValueError("--hosts requires a path")
            hosts_path = Path(argv[index + 1])
            index += 2
        elif option == "--connect-timeout":
            if index + 1 >= len(argv):
                raise ValueError("--connect-timeout requires seconds")
            value = argv[index + 1]
            if not value.isdigit() or int(value) < 1:
                raise ValueError("--connect-timeout must be a positive integer")
            connect_timeout_s = int(value)
            index += 2
        elif option == "-l":
            if user is not None:
                raise ValueError("-l specified more than once")
            if index + 1 >= len(argv):
                raise ValueError("-l requires a user")
            user = argv[index + 1]
            if not _NAME_RE.fullmatch(user):
                raise ValueError(f"invalid SSH user {user!r}")
            index += 2
        elif option == "--":
            index += 1
            break
        elif option.startswith("-"):
            raise ValueError(f"unknown option {option!r}")
        else:
            break
    if hosts_path is None:
        raise ValueError("missing --hosts inventory path")
    if index >= len(argv):
        raise ValueError("missing host name")
    token = argv[index]
    remote = argv[index + 1:]
    if not remote:
        raise ValueError(f"{token}: missing remote command")
    return hosts_path, connect_timeout_s, user, token, remote


def _address_candidates(hosts: tuple[Host, ...], address: str) -> list[Host]:
    folded = address.casefold()
    exact = [host for host in hosts if host.address.casefold() == folded]
    if exact:
        return exact
    # Open MPI 4 strips the domain of --host/hostfile names unless
    # orte_keep_fqdn_hostnames is set; a unique inventory short label is the
    # only remaining interpretation of the token. Never consult DNS.
    if "." in address:
        return []
    return [host for host in hosts if host.address.split(".", 1)[0].casefold() == folded]


def _select_host(hosts: tuple[Host, ...], token: str, requested_user: str | None) -> Host:
    token_user, separator, address = token.rpartition("@")
    if not separator:
        token_user, address = None, token
    if token_user is not None and requested_user is not None and token_user != requested_user:
        raise ValueError(f"{token}: conflicting users {token_user!r} and {requested_user!r}")
    user = requested_user if requested_user is not None else token_user
    candidates = _address_candidates(hosts, address)
    if not candidates:
        known = ", ".join(sorted({host.address for host in hosts})) or "none"
        raise ValueError(f"unknown host {token!r} (known addresses: {known})")
    if user is None:
        if len(candidates) != 1:
            raise ValueError(f"ambiguous host {token!r}: {len(candidates)} inventory entries")
        return candidates[0]
    matches = [host for host in candidates if host.user == user]
    if not matches:
        if any(host.user is not None for host in candidates):
            raise ValueError(f"{token}: user does not match the host inventory")
        if len(candidates) != 1:
            raise ValueError(f"ambiguous host {token!r}: {len(candidates)} inventory entries")
        matches = [replace(candidates[0], user=user)]
    if len(matches) != 1:
        raise ValueError(f"ambiguous host {token!r}: {len(matches)} inventory entries")
    return matches[0]


def _agent_main(argv: list[str]) -> int:
    """Open MPI plm_rsh_agent protocol: [options] <host> <remote argv...>."""
    try:
        hosts_path, connect_timeout_s, user, token, remote = _parse_agent_args(argv)
        hosts = load_hosts(hosts_path)
        host = _select_host(hosts, token, user)
        command = ssh_argv(host, connect_timeout_s)
        os.execvp(command[0], [*command, *remote])
    except (OSError, ValueError) as exc:
        print(f"ssh.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_agent_main(sys.argv[1:]))
