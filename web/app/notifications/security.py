from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


class DestinationRejected(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedDestination:
    url: str
    host: str
    pinned_ip: str | None


BLOCKED_HOSTS = {
    "metadata", "metadata.google.internal", "metadata.azure.internal", "instance-data.ec2.internal",
}


def configured_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    raw = os.environ.get("NOTIFICATION_ALLOWED_DESTINATION_CIDRS", "")
    private_ranges = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
    )
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise DestinationRejected("NOTIFICATION_ALLOWED_DESTINATION_CIDRS contains an invalid CIDR") from exc
            if not any(network.version == private.version and network.subnet_of(private) for private in private_ranges):
                raise DestinationRejected("Allowed destination CIDRs must be contained in RFC1918 or IPv6 ULA ranges")
            networks.append(network)
    return tuple(networks)


def resolve_host(host: str) -> set[ipaddress._BaseAddress]:
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DestinationRejected("Destination host could not be resolved") from exc
    addresses = set()
    for result in results:
        try:
            addresses.add(ipaddress.ip_address(result[4][0].split("%", 1)[0]))
        except ValueError:
            continue
    if not addresses:
        raise DestinationRejected("Destination host has no usable address")
    return addresses


def validate_destination_url(
    url: str,
    *,
    allowed_networks: tuple[ipaddress._BaseNetwork, ...] | None = None,
    resolved_addresses: set[ipaddress._BaseAddress] | None = None,
) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise DestinationRejected("Destination URL is invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DestinationRejected("Destination URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DestinationRejected("Destination URL must use HTTP or HTTPS and include a host")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise DestinationRejected("Destination URL cannot contain user information or a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise DestinationRejected("Destination port is invalid")
    host = parsed.hostname.rstrip(".").lower()
    if host in BLOCKED_HOSTS or host.endswith(".localhost") or host == "localhost":
        raise DestinationRejected("Destination host is blocked")

    networks = configured_networks() if allowed_networks is None else allowed_networks
    try:
        # An address literal gives the operator an unambiguous LAN target.
        # Hostnames are treated separately below because DNS can change between
        # validation and the HTTP client's connection attempt.
        literal = ipaddress.ip_address(host)
        is_literal = True
        addresses = {literal}
    except ValueError:
        is_literal = False
        addresses = resolved_addresses if resolved_addresses is not None else resolve_host(host)

    private_destination = False
    for address in addresses:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast or address.is_reserved:
            raise DestinationRejected("Loopback, link-local, reserved and metadata destinations are blocked")
        if address.is_private:
            if not is_literal:
                raise DestinationRejected("Hostnames resolving to private addresses are blocked; use an allowed IP literal for LAN services")
            if not any(address.version == network.version and address in network for network in networks):
                raise DestinationRejected("Private IP literals require an explicit allowed CIDR")
            private_destination = True
        elif not address.is_global:
            raise DestinationRejected("Destination address is not globally routable")
    if private_destination and parsed.scheme != "https":
        # LAN self-hosted providers can use plain HTTP only after their CIDR is
        # explicitly allowed; credentials still travel on the trusted LAN.
        pass
    elif parsed.scheme != "https":
        raise DestinationRejected("Public notification destinations must use HTTPS")
    return url


def prepare_destination_url(url: str) -> ResolvedDestination:
    """Validate once and retain the resolved address for a pinned connection.

    Domain names are resolved here, checked against the SSRF policy, and later
    connected by IP while preserving the original HTTP Host and TLS SNI. This
    closes the DNS-rebinding gap between validation and connection.
    """
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            raise DestinationRejected("Destination URL must include a host")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            validate_destination_url(url)
            return ResolvedDestination(url, host, None)
        normalized_host = host.encode("idna").decode("ascii").rstrip(".").lower()
        addresses = resolve_host(normalized_host)
        validate_destination_url(url, resolved_addresses=addresses)
        selected = sorted(addresses, key=lambda address: (address.version, int(address)))[0]
        return ResolvedDestination(url, normalized_host, str(selected))
    except UnicodeError as exc:
        raise DestinationRejected("Destination hostname is invalid") from exc
