"""External sorting: chunked in-memory runs plus bounded fan-in merge waves.

The sorter never holds more than ``chunk_size`` entries while forming a run
and never holds more than one head frame per input run while merging.  When
more than ``fan_in`` runs exist, merging happens in repeated waves, so the
number of simultaneously open run files is bounded by ``fan_in`` regardless
of how many runs the input produced.  Nothing here grows with the total row
count or with the size of one key group.
"""

import heapq
import os
import tempfile

from .frame import FrameReader, FrameWriter


class ExternalSorter:
    """Collect framed entries, spill sorted chunks, merge with limited fan-in.

    Parameters
    ----------
    work_dir:
        Directory for every temporary run file.
    prefix:
        Filename prefix, keeping the left, right and pair stages apart.
    chunk_size:
        Maximum number of entries buffered in memory before a run is spilled.
    fan_in:
        Maximum number of runs merged in one pass (>= 2).
    """

    def __init__(self, work_dir, prefix, chunk_size, fan_in, chunk_bytes=None):
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        if fan_in < 2:
            raise ValueError("fan_in must be >= 2")
        if chunk_bytes is not None and chunk_bytes < 1:
            raise ValueError("chunk_bytes must be >= 1 when provided")
        self._work_dir = work_dir
        self._prefix = prefix
        self._chunk_size = int(chunk_size)
        self._chunk_bytes = chunk_bytes
        self._fan_in = int(fan_in)
        self._buffer = []
        self._buffered_bytes = 0
        self._run_paths = []
        self._all_paths = set()
        self._serial = 0
        self.waves = 0
        self.initial_run_count = 0

    # -- spilling ---------------------------------------------------------

    def add(self, magic, key, body):
        """Buffer one framed entry; spill a run once a chunk limit is hit.

        A run is spilled when either the record count reaches ``chunk_size``
        or the buffered frame bytes reach ``chunk_bytes`` (if configured), so
        memory stays bounded even when individual payloads are very large.
        """
        self._buffer.append((key, magic, body))
        self._buffered_bytes += len(key) + len(body)
        if len(self._buffer) >= self._chunk_size or (
            self._chunk_bytes is not None
            and self._buffered_bytes >= self._chunk_bytes
        ):
            self.flush_chunk()

    def flush_chunk(self):
        if not self._buffer:
            return
        # Component-wise key order, then body, gives a deterministic run even
        # for byte-identical duplicate records (each stays a separate entry).
        self._buffer.sort(key=lambda item: (item[0], item[1], item[2]))
        path = self._new_path("run")
        with open(path, "wb", buffering=1 << 16) as fh:
            writer = FrameWriter(fh)
            for key, magic, body in self._buffer:
                writer.write(magic, key, body)
        self._run_paths.append(path)
        self.initial_run_count += 1
        self._buffer = []
        self._buffered_bytes = 0

    def _new_path(self, tag):
        self._serial += 1
        fd, path = tempfile.mkstemp(
            prefix=f"{self._prefix}_{tag}_{self._serial}_",
            suffix=".fr",
            dir=self._work_dir,
        )
        os.close(fd)
        self._all_paths.add(path)
        return path

    @property
    def run_count(self):
        return len(self._run_paths)

    # -- merge waves ------------------------------------------------------

    def finalize(self):
        """Merge all spilled runs in waves; return the final run path.

        Returns ``None`` when no entry was ever added.  At most ``fan_in``
        run files are open at the same time, even if hundreds of runs exist.
        """
        self.flush_chunk()
        if not self._run_paths:
            return None
        wave = 0
        current = list(self._run_paths)
        while len(current) > 1:
            wave += 1
            next_runs = []
            for start in range(0, len(current), self._fan_in):
                batch = current[start:start + self._fan_in]
                merged_path = self._merge_one_wave(batch, wave)
                next_runs.append(merged_path)
                for consumed in batch:
                    try:
                        os.remove(consumed)
                    except OSError:
                        pass
            current = next_runs
        self._run_paths = current
        self.waves = wave
        return current[0]

    def _merge_one_wave(self, run_paths, wave):
        output_path = self._new_path(f"wave{wave}")
        readers = []
        heap = []
        try:
            for seq, path in enumerate(run_paths):
                fh = open(path, "rb", buffering=1 << 16)
                reader = FrameReader(fh)
                readers.append((fh, reader))
                frame = reader.read_frame()
                if frame is not None:
                    magic, key, body, _ = frame
                    # Sequence number breaks ties without comparing file
                    # objects; equal keys may come out in any run order.
                    heapq.heappush(heap, (key, seq, magic, body))
            with open(output_path, "wb", buffering=1 << 16) as out_fh:
                writer = FrameWriter(out_fh)
                while heap:
                    key, seq, magic, body = heapq.heappop(heap)
                    writer.write(magic, key, body)
                    reader = readers[seq][1]
                    frame = reader.read_frame()
                    if frame is not None:
                        magic, key, body, _ = frame
                        heapq.heappush(heap, (key, seq, magic, body))
        finally:
            for fh, _reader in readers:
                fh.close()
        return output_path

    def cleanup(self, path=None):
        """Remove every run file this sorter ever created (best effort)."""
        candidates = set(self._all_paths)
        if path is not None:
            candidates.add(path)
        for p in candidates:
            try:
                os.remove(p)
            except OSError:
                pass
        self._run_paths = []
        self._all_paths.clear()
