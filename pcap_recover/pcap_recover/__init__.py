"""pcap_recover: offline reconstruction of HTTP response bodies from PCAP.

The package never opens a network socket.  Its public entry point is
:func:`pcap_recover.extract.run_recovery` or the equivalent
``python -m pcap_recover.cli`` command.
"""

__version__ = "1.0.0"

from .errors import (
    ConfigError,
    ConnectionFailure,
    FatalInputError,
    HttpParseError,
    LimitExceeded,
    PcapRecoverError,
)
from .extract import run_recovery

__all__ = [
    "run_recovery",
    "ConfigError",
    "ConnectionFailure",
    "FatalInputError",
    "HttpParseError",
    "LimitExceeded",
    "PcapRecoverError",
    "__version__",
]
