"""Classic PCAP container reader.

Supported scope, deliberately narrow:

* classic PCAP only (the ``\\xa1\\xb2\\xc3\\xd4`` / ``\\xd4\\xc3\\xb2\\xa1``
  magic families), microsecond timestamps, both byte orders;
* linktype Ethernet (``DLT_EN10MB`` = 1).

A short global header, a short packet header, a short packet record, an
unknown magic or an unsupported link type all raise
:class:`~pcap_recover.errors.FatalInputError`.  A truncated file is never
treated as a clean end of input.
"""

import struct

from .errors import FatalInputError

_MAGIC_MICRO_BE = 0xA1B2C3D4
_MAGIC_MICRO_LE = 0xD4C3B2A1
_MAGIC_NANO_BE = 0xA1B2CD34
_MAGIC_NANO_LE = 0x34CDB2A1

_GLOBAL_HEADER_SIZE = 24
_PACKET_HEADER_SIZE = 16
_LINKTYPE_ETHERNET = 1


class PcapReader:
    """Iterate packet records of a classic microsecond PCAP file."""

    def __init__(self, stream):
        self._stream = stream
        self._read_global_header()

    def _read_global_header(self):
        blob = self._stream.read(_GLOBAL_HEADER_SIZE)
        if len(blob) == 0:
            raise FatalInputError("pcap_global_truncated", "empty file")
        if len(blob) < _GLOBAL_HEADER_SIZE:
            raise FatalInputError(
                "pcap_global_truncated",
                f"global header is {len(blob)} bytes, expected "
                f"{_GLOBAL_HEADER_SIZE}",
            )
        magic = struct.unpack(">I", blob[:4])[0]
        if magic in (_MAGIC_MICRO_BE, _MAGIC_MICRO_LE):
            self.big_endian = magic == _MAGIC_MICRO_BE
        elif magic in (_MAGIC_NANO_BE, _MAGIC_NANO_LE):
            raise FatalInputError(
                "pcap_unsupported",
                "nanosecond-resolution PCAP is outside the supported scope "
                "(microsecond timestamps only)",
            )
        else:
            raise FatalInputError("pcap_bad_magic", "not a classic PCAP file")

        endian = ">" if self.big_endian else "<"
        version_major, version_minor, _tz, _sigfigs, snaplen, network = (
            struct.unpack(endian + "HHIIII", blob[4:24])
        )
        if (version_major, version_minor) != (2, 4):
            raise FatalInputError(
                "pcap_unsupported",
                f"unsupported PCAP version {version_major}.{version_minor}",
            )
        if network != _LINKTYPE_ETHERNET:
            raise FatalInputError(
                "pcap_unsupported_linktype",
                f"linktype {network} is not Ethernet (1)",
            )
        self.snaplen = snaplen
        self._header_struct = struct.Struct(endian + "IIII")

    def __iter__(self):
        return self

    def __next__(self):
        head = self._stream.read(_PACKET_HEADER_SIZE)
        if len(head) == 0:
            raise StopIteration
        if len(head) < _PACKET_HEADER_SIZE:
            raise FatalInputError(
                "pcap_packet_header_truncated",
                f"packet header is {len(head)} bytes, expected "
                f"{_PACKET_HEADER_SIZE}",
            )
        ts_sec, ts_frac, incl_len, orig_len = self._header_struct.unpack(head)
        if incl_len == 0:
            # A zero-length record is still a valid (if useless) record;
            # orig_len == 0 must accompany it.
            if orig_len != 0:
                raise FatalInputError(
                    "pcap_bad_record",
                    "record stores 0 bytes but claims a non-zero original "
                    "length",
                )
            data = b""
        else:
            data = self._stream.read(incl_len)
            if len(data) < incl_len:
                raise FatalInputError(
                    "pcap_packet_record_truncated",
                    f"record needs {incl_len} bytes, only {len(data)} "
                    "follow the header",
                )
        # ts_frac is microseconds by construction (nano magic is rejected).
        return {
            "ts_sec": ts_sec,
            "ts_usec": ts_frac,
            "incl_len": incl_len,
            "orig_len": orig_len,
            "data": data,
        }
