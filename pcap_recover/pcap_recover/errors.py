"""Exception hierarchy for the offline PCAP collection command.

Two failure layers matter operationally:

* :class:`FatalInputError` -- the PCAP container itself is structurally
  damaged (truncated file/header/record) or uses an unsupported container
  variant.  No result of the current run may be published.
* :class:`ConnectionFailure` -- a single selected TCP connection cannot be
  reconstructed or parsed.  Other connections remain recoverable; the
  failure reason is recorded in the total index.
"""


class PcapRecoverError(Exception):
    """Base class for all errors raised by this package."""


class ConfigError(PcapRecoverError):
    """The endpoint configuration or command line arguments are invalid."""

    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


class FatalInputError(PcapRecoverError):
    """Structural damage of the input file; the whole run must abort."""

    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


class HttpParseError(PcapRecoverError):
    """HTTP/1.1 framing or content-encoding rules were violated."""

    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail
        message = reason if not detail else f"{reason}: {detail}"
        super().__init__(message)


class LimitExceeded(PcapRecoverError):
    """A configurable resource limit (connections/stream/body) was hit."""

    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail
        message = reason if not detail else f"{reason}: {detail}"
        super().__init__(message)


class ConnectionFailure(PcapRecoverError):
    """A selected connection cannot be extracted.

    ``reason`` is a stable machine-readable code (see README) and ``detail``
    carries human-readable diagnostic information.
    """

    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail
        message = reason if not detail else f"{reason}: {detail}"
        super().__init__(message)
