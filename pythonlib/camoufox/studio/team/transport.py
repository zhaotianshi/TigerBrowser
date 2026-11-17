"""Direct-peer network scopes, independent of DNS and proxy headers."""

from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

PRIVATE_NETWORKS = tuple(ip_network(value) for value in
                         ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))


def exact_origin(origin):
    """An HTTP(S) origin exactly as a browser sends it in the Origin header."""
    parsed = urlsplit(origin) if isinstance(origin, str) else None
    if (parsed is None or parsed.scheme not in ('https', 'http') or not parsed.hostname
            or parsed.username is not None or parsed.path or parsed.query or parsed.fragment
            or origin != f'{parsed.scheme}://{parsed.netloc}' or any(char.isspace() for char in origin)
            or '\\' in origin):
        raise ValueError('Use an exact HTTP or HTTPS origin without a path or credentials.')
    return parsed


def session_cookie(scheme):
    """The login cookie's name; browsers accept the __Host- prefix only over HTTPS."""
    return '__Host-camoufox-team' if scheme == 'https' else 'camoufox-team-local'


def private_address(value, *, allow_loopback=False):
    try:
        address = ip_address(value)
    except ValueError:
        return False
    return (allow_loopback and address.is_loopback or
            any(address in network for network in PRIVATE_NETWORKS))


def client_allowlist(values):
    if values is None:
        return ()
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValueError('Client allowlist entries must be IP addresses or CIDR networks.')
    networks = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError('Client allowlist entries must be IP addresses or CIDR networks.')
        try:
            networks.append(ip_network(value, strict=False))
        except ValueError:
            raise ValueError('Client allowlist entries must be IP addresses or CIDR networks.') from None
    return tuple(networks)


def allowed_client(value, networks):
    try:
        address = ip_address(value)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address.version == network.version and address in network for network in networks)
