"""Endpoint configuration loading.

The configuration is a small JSON document naming the server endpoints that
are allowed to be recovered.  Only packets *from* these (IP, port) pairs
towards clients are tracked::

    {
      "endpoints": [
        {"ip": "10.0.0.5", "port": 8080},
        {"ip": "2001:db8::1", "port": 80}
      ]
    }

Only IPv4 endpoints can match the supported captures; a syntactically valid
IPv6 entry is kept but never matches Ethernet/IPv4 traffic.
"""

import ipaddress
import json

from .errors import ConfigError


def load_endpoints(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except OSError as exc:
        raise ConfigError("cannot read endpoint configuration", str(exc))
    except json.JSONDecodeError as exc:
        raise ConfigError("endpoint configuration is not valid JSON", str(exc))

    if not isinstance(doc, dict) or not isinstance(doc.get("endpoints"), list):
        raise ConfigError(
            "config_shape",
            'configuration must be an object with an "endpoints" array',
        )

    endpoints = []
    seen = set()
    for index, entry in enumerate(doc["endpoints"]):
        if not isinstance(entry, dict):
            raise ConfigError(
                "config_shape", f"endpoints[{index}] must be an object"
            )
        ip_text = entry.get("ip")
        port = entry.get("port")
        if not isinstance(ip_text, str) or not isinstance(port, int):
            raise ConfigError(
                "config_shape",
                f"endpoints[{index}] needs string 'ip' and integer 'port'",
            )
        if isinstance(port, bool) or not (0 < port < 65536):
            raise ConfigError(
                "config_shape",
                f"endpoints[{index}] port {port} is outside 1..65535",
            )
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError:
            raise ConfigError(
                "config_shape", f"endpoints[{index}] has a bad IP: {ip_text}"
            )
        pair = (str(address), port)
        if pair in seen:
            raise ConfigError(
                "config_shape", f"duplicate endpoint entry: {pair}"
            )
        seen.add(pair)
        endpoints.append(pair)

    if not endpoints:
        raise ConfigError("config_shape", "at least one endpoint is required")
    return endpoints
