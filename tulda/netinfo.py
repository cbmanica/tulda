import os
import socket
import subprocess


def lan_ipv4() -> str:
    """IPv4 of the default-route interface (no packets are sent); 127.0.0.1 on failure."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
        return ip if not ip.startswith("127.") and ip != "0.0.0.0" else "127.0.0.1"
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def bonjour_name() -> str:
    try:
        p = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True,
                           text=True, timeout=5)
        n = p.stdout.strip()
        if p.returncode == 0 and n:
            return f"{n}.local"
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname().split(".")[0] + ".local"


def local_label() -> str | None:
    """`scutil --get LocalHostName` (macOS), or None."""
    try:
        p = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True,
                           text=True, timeout=5)
        n = p.stdout.strip()
        return n if p.returncode == 0 and n else None
    except (OSError, subprocess.SubprocessError):
        return None


def host_aliases(local: str | None, hostname: str | None, magic: str | None) -> list[str]:
    """Lower-case Host header names that refer to this machine: bare short names,
    `<short>.local`, and the full MagicDNS name. Pure; inputs come from the runtime."""
    out: list[str] = []

    def add(n: str) -> None:
        n = n.strip().rstrip(".").lower()
        if n and n not in out:
            out.append(n)

    for n in (local, hostname):
        if n:
            short = n.strip().split(".")[0]
            if short:
                add(short)
                add(short + ".local")
    if magic:
        add(magic)
        add(magic.strip().split(".")[0])
    return out


def local_host_names(magic: str | None = None) -> list[str]:
    """Names this machine answers to, derived at runtime (see host_aliases)."""
    return host_aliases(local_label(), socket.gethostname(), magic)


LOOPBACK = "127.0.0.1"   # always bound in addition to the LAN and tailnet addresses

TAILSCALE_CLI = ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def is_wildcard_host(h: str) -> bool:
    """True for anything that would bind every interface: empty, `0`, or any
    unspecified address (IPv4 or IPv6, any spelling)."""
    import ipaddress
    h = h.strip().strip("[]").split("%")[0]
    if not h or h == "0":
        return True
    try:
        return ipaddress.ip_address(h).is_unspecified
    except ValueError:
        return False


def parse_hosts(items) -> list[str]:
    """Flatten repeatable/comma-separated --host values, dropping duplicates.
    Wildcard/empty values are refused: tulda only binds the addresses it is given."""
    from .errors import ConfigError
    out: list[str] = []
    for it in items or []:
        for h in str(it).split(","):
            h = h.strip()
            if is_wildcard_host(h):
                raise ConfigError(f"refusing wildcard/empty --host {h!r}: tulda never binds all "
                                  "interfaces; give a specific interface address")
            if h not in out:
                out.append(h)
    return out


def in_cgnat(ip: str) -> bool:
    import ipaddress
    try:
        return ipaddress.IPv4Address(ip) in ipaddress.IPv4Network("100.64.0.0/10")
    except ValueError:
        return False


def _tailscale(*args: str) -> str | None:
    import shutil
    for cli in TAILSCALE_CLI:
        exe = shutil.which(cli) if "/" not in cli else (cli if os.path.exists(cli) else None)
        if not exe:
            continue
        try:
            p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout
    return None


def scan_cgnat_ipv4() -> str | None:
    """First interface address in 100.64.0.0/10 (Tailscale), from `ifconfig`."""
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "inet" and in_cgnat(parts[1]):
            return parts[1]
    return None


def tailscale_ipv4() -> str | None:
    out = _tailscale("ip", "-4")
    if out:
        for line in out.splitlines():
            if in_cgnat(line.strip()):
                return line.strip()
    return scan_cgnat_ipv4()


def tailscale_dnsname() -> str | None:
    """MagicDNS name of this node (Self.DNSName without the trailing dot), if known."""
    import json
    out = _tailscale("status", "--json")
    if not out:
        return None
    try:
        n = (json.loads(out).get("Self") or {}).get("DNSName") or ""
    except ValueError:
        return None
    n = n.rstrip(".").lower()
    return n or None
