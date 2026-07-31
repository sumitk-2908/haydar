"""Benchmark IndexingEngine.index_all() throughput and peak memory.

Usage:
    python scripts/bench_index.py [--files N] [--embedding-batch-size N]

Note: VectorStore (the embedding model) is mocked so this measures the crawl,
extraction and chunking pipeline only -- not embedding memory. The reported
memory figure therefore reflects file I/O + chunking overhead, not end-to-end
RSS. Peak RSS is sampled continuously during the run via a background thread
(psutil); without psutil we fall back to tracemalloc's peak traced allocation.
"""
import argparse
import os
import platform
import random
import shutil
import string
import tempfile
import threading
import time
from unittest.mock import patch

try:
    import psutil  # type: ignore
    HAS_PSUTIL = True
except ImportError:
    import tracemalloc
    HAS_PSUTIL = False

from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexingEngine


class _PeakRSSSampler(threading.Thread):
    """Poll process RSS on an interval and retain the maximum observed value."""

    def __init__(self, interval: float = 0.02):
        super().__init__(daemon=True)
        self._interval = interval
        self._stop_event = threading.Event()
        self._proc = psutil.Process()
        self.baseline = self._proc.memory_info().rss
        self.peak = self.baseline

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.peak = max(self.peak, self._proc.memory_info().rss)
            self._stop_event.wait(self._interval)

    def stop(self) -> int:
        self._stop_event.set()
        self.join()
        self.peak = max(self.peak, self._proc.memory_info().rss)
        return self.peak


def run_benchmark(files: int, batch_size: int | None = None):
    config = HaydarConfig(initialized=True)
    if batch_size is not None:
        config.embedding_batch_size = batch_size

    temp_dir = tempfile.mkdtemp(prefix="haydar_bench_")
    sampler = None
    measurement_started = False
    try:
        config.folders = [temp_dir]
        print(f"Generating {files} files in {temp_dir}...")
        total_bytes = 0
        vocab = " ".join(
            "".join(random.choices(string.ascii_lowercase, k=random.randint(3, 10)))
            for _ in range(100)
        ).split()

        for i in range(files):
            size = random.randint(1024, 512 * 1024)
            total_bytes += size
            path = os.path.join(temp_dir, f"file_{i}.txt")
            chunk = " ".join(random.choices(vocab, k=100))
            repeats = (size // len(chunk)) + 1
            content = (chunk * repeats)[:size]
            with open(path, "w", encoding="utf-8") as file:
                file.write(content)

        print(f"Total size: {total_bytes / (1024 * 1024):.2f} MB")

        if HAS_PSUTIL:
            sampler = _PeakRSSSampler()
            sampler.start()
        else:
            tracemalloc.start()
        measurement_started = True

        with patch("haydar.indexer.engine.VectorStore") as mock_store_cls:
            mock_store_cls.return_value.get_stats.return_value = {
                "files_indexed": 0,
                "chunks_stored": 0,
                "db_size_bytes": 0,
            }
            with IndexingEngine(config, allow_download=False) as engine:
                start_time = time.perf_counter()
                engine.index_all()
                end_time = time.perf_counter()
    finally:
        if measurement_started and HAS_PSUTIL:
            peak_rss = sampler.stop()
            baseline_rss = sampler.baseline
            mem_label = "Peak RSS (sampled)"
        elif measurement_started:
            baseline_rss = 0
            peak_rss = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            mem_label = "Peak traced alloc"

        try:
            shutil.rmtree(temp_dir)
        except OSError as exc:
            raise RuntimeError(
                f"Benchmark cleanup failed; remove temporary tree manually: {temp_dir}"
            ) from exc

    wall_time = end_time - start_time
    peak_mb = peak_rss / (1024 * 1024)
    delta_mb = (peak_rss - baseline_rss) / (1024 * 1024)
    files_per_sec = files / wall_time if wall_time > 0 else 0
    mb_per_sec = (total_bytes / (1024 * 1024)) / wall_time if wall_time > 0 else 0

    print(f"Batch size: {config.embedding_batch_size}")
    print(f"Wall time: {wall_time:.2f} s")
    print(f"Platform: {platform.platform()}")
    if HAS_PSUTIL:
        installed_gb = psutil.virtual_memory().total / (1024**3)
        print(f"Installed RAM: {installed_gb:.2f} GB")
    print(f"{mem_label}: {peak_mb:.2f} MB (delta over baseline: {delta_mb:.2f} MB)")
    print(f"Throughput: {files_per_sec:.2f} files/s, {mb_per_sec:.2f} MB/s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Benchmark IndexingEngine.index_all() throughput and peak memory."
    )
    parser.add_argument("--files", type=int, default=5000, help="Number of files to generate (default: 5000)")
    parser.add_argument("--embedding-batch-size", type=int, default=None, help="Embedding batch size (default: from config)")
    args = parser.parse_args()

    run_benchmark(args.files, args.embedding_batch_size)
