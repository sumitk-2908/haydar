"""Run reproducible Haydar indexing and semantic-search benchmarks.

Examples:
    python scripts/bench_index.py --files 10000 --embedding-batch-size 100
    python scripts/bench_index.py --store real --corpus C:\\benchmark-corpus \
        --data-dir C:\\haydar-benchmark-data --queries queries.txt --repeat 3 \
        --output benchmark.json

The default ``mocked`` store mode measures crawl, extraction, and chunking only.
Use ``--store real`` for publishable end-to-end indexing and semantic-search
measurements. A user-provided corpus is read-only and is never deleted.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import random
import shutil
import statistics
import string
import sys
import tempfile
import threading
import time
import tracemalloc
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

try:
    import psutil  # type: ignore

    HAS_PSUTIL = True
except ImportError:
    import tracemalloc

    HAS_PSUTIL = False

from haydar import __version__
from haydar.config import HaydarConfig
from haydar.indexer.engine import IndexingEngine

DEFAULT_QUERIES = (
    "quarterly budget projections",
    "meeting notes and action items",
    "installation troubleshooting instructions",
    "project architecture overview",
    "performance benchmark results",
)


@dataclass(frozen=True)
class RunResult:
    run: int
    wall_time_seconds: float
    peak_rss_mb: float
    rss_delta_mb: float
    files_per_second: float
    megabytes_per_second: float
    files_indexed: int
    chunks_stored: int
    database_size_mb: float
    idle_search_rss_mb: float | None
    cold_search_ms: float | None
    warm_search_median_ms: float | None
    warm_search_p95_ms: float | None


class _NullVectorStore:
    """No-op store used to isolate the indexing pipeline from embeddings."""

    def add_documents(self, ids: list[str], documents: list[str], metadatas: list[dict]) -> None:
        return None

    def delete_by_filepaths(self, filepaths: list[str]) -> None:
        return None

    def get_stats(self) -> dict[str, int]:
        return {"files_indexed": 0, "chunks_stored": 0, "db_size_bytes": 0}


class _PeakRSSSampler(threading.Thread):
    """Poll process RSS and retain the maximum observed value."""

    def __init__(self, interval: float = 0.02) -> None:
        super().__init__(daemon=True)
        self._interval = interval
        self._stop_event = threading.Event()
        self._process = psutil.Process()
        self.baseline = self._process.memory_info().rss
        self.peak = self.baseline

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.peak = max(self.peak, self._process.memory_info().rss)
            self._stop_event.wait(self._interval)

    def stop(self) -> tuple[int, int]:
        self._stop_event.set()
        self.join()
        self.peak = max(self.peak, self._process.memory_info().rss)
        return self.baseline, self.peak


def percentile(values: Sequence[float], percentile_value: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sequence."""
    if not values:
        raise ValueError("Cannot calculate a percentile of an empty sequence.")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_value / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def directory_size(path: Path) -> int:
    """Return regular-file bytes below path without following symlinks."""
    total = 0
    if not path.exists():
        return total
    for candidate in path.rglob("*"):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                total += candidate.stat().st_size
        except OSError:
            continue
    return total


def corpus_stats(path: Path) -> tuple[int, int]:
    """Return the count and bytes of indexable regular files in a corpus."""
    from haydar.config import ALL_INDEXABLE_EXTENSIONS

    count = 0
    total_bytes = 0
    for candidate in path.rglob("*"):
        try:
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and candidate.suffix.lower() in ALL_INDEXABLE_EXTENSIONS
            ):
                count += 1
                total_bytes += candidate.stat().st_size
        except OSError:
            continue
    return count, total_bytes


def generate_corpus(path: Path, files: int, seed: int) -> None:
    """Generate deterministic text files in an empty benchmark directory."""
    rng = random.Random(seed)
    vocabulary = [
        "".join(rng.choices(string.ascii_lowercase, k=rng.randint(3, 10)))
        for _ in range(100)
    ]
    for index in range(files):
        size = rng.randint(1024, 512 * 1024)
        words = " ".join(rng.choices(vocabulary, k=100))
        repeats = (size // len(words)) + 1
        (path / f"file_{index:06d}.txt").write_text(
            (words * repeats)[:size], encoding="utf-8"
        )


def load_queries(path: Path | None) -> list[str]:
    """Load non-empty query lines, or return the documented default set."""
    if path is None:
        return list(DEFAULT_QUERIES)
    queries = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    queries = [query for query in queries if query]
    if not queries:
        raise ValueError(f"Query file is empty: {path}")
    return queries


def configure_data_dir(data_dir: Path) -> None:
    """Redirect every imported Haydar storage path to an isolated directory."""
    import haydar.config as config_module
    import haydar.indexer.cache as cache_module
    import haydar.indexer.extractors as extractors_module
    import haydar.search.store as store_module

    paths = {
        "HAYDAR_DIR": data_dir,
        "CONFIG_PATH": data_dir / "config.json",
        "DB_DIR": data_dir / "db",
        "LOG_DIR": data_dir / "logs",
        "MODELS_DIR": data_dir / "models",
        "CACHE_DIR": data_dir / "cache",
        "RIPGREP_DIR": data_dir / "bin",
        "INDEX_LOCK": data_dir / ".indexing.lock",
    }
    for name, value in paths.items():
        setattr(config_module, name, value)
    cache_module.DB_DIR = paths["DB_DIR"]
    extractors_module.CACHE_DIR = paths["CACHE_DIR"]
    store_module.DB_DIR = paths["DB_DIR"]
    store_module.MODELS_DIR = paths["MODELS_DIR"]

    for value in paths.values():
        if value.suffix:
            value.parent.mkdir(parents=True, exist_ok=True)
        else:
            value.mkdir(parents=True, exist_ok=True)


def reset_run_data(data_dir: Path) -> None:
    """Reset index/cache state while retaining a previously downloaded model."""
    for child in ("db", "cache"):
        path = data_dir / child
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
    lock_path = data_dir / ".indexing.lock"
    lock_path.unlink(missing_ok=True)


def _start_memory_measurement() -> tuple[_PeakRSSSampler | None, int]:
    if HAS_PSUTIL:
        sampler = _PeakRSSSampler()
        sampler.start()
        return sampler, sampler.baseline
    tracemalloc.start()
    return None, 0


def _stop_memory_measurement(sampler: _PeakRSSSampler | None) -> tuple[int, int]:
    if HAS_PSUTIL:
        if sampler is None:
            raise RuntimeError("RSS sampler was not started.")
        return sampler.stop()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return 0, peak


def measure_searches(config: HaydarConfig, queries: Sequence[str]) -> tuple[float, float, float]:
    """Measure one cold and all remaining warm semantic first-result latencies."""
    from haydar.search.hybrid import HybridSearch

    search = HybridSearch(config)
    latencies: list[float] = []
    expanded_queries = list(queries)
    while len(expanded_queries) < 31:
        expanded_queries.extend(queries)
    expanded_queries = expanded_queries[:31]

    for query in expanded_queries:
        started = time.perf_counter()
        first_result_seen = False
        for result_batch in search.search_stream(query, limit=config.results_limit, mode="semantic"):
            if result_batch:
                first_result_seen = True
                break
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not first_result_seen:
            raise RuntimeError(f"Semantic benchmark query returned no results: {query!r}")
        latencies.append(elapsed_ms)

    warm = latencies[1:]
    return latencies[0], statistics.median(warm), percentile(warm, 95)


def run_once(
    *,
    run_number: int,
    store_mode: str,
    corpus: Path,
    total_bytes: int,
    config: HaydarConfig,
    queries: Sequence[str],
    data_dir: Path,
) -> RunResult:
    """Execute one clean indexing run and optional semantic-search phase."""
    reset_run_data(data_dir)
    sampler, _ = _start_memory_measurement()
    store_patch = (
        patch("haydar.indexer.engine.VectorStore", return_value=_NullVectorStore())
        if store_mode == "mocked"
        else nullcontext()
    )

    try:
        with store_patch, IndexingEngine(
            config, allow_download=store_mode == "real"
        ) as engine:
            started = time.perf_counter()
            stats = engine.index_all(force=True)
            wall_time = time.perf_counter() - started
            store_stats = engine.store.get_stats() if store_mode == "real" else {}
    finally:
        baseline_rss, peak_rss = _stop_memory_measurement(sampler)

    idle_search_rss_mb: float | None = None
    cold_ms: float | None = None
    warm_median_ms: float | None = None
    warm_p95_ms: float | None = None
    if store_mode == "real":
        if HAS_PSUTIL:
            idle_search_rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        cold_ms, warm_median_ms, warm_p95_ms = measure_searches(config, queries)

    files_indexed = int(stats.get("files_indexed", 0))
    chunks_stored = int(stats.get("chunks_stored", 0))
    database_bytes = int(store_stats.get("db_size_bytes", directory_size(data_dir / "db")))
    return RunResult(
        run=run_number,
        wall_time_seconds=wall_time,
        peak_rss_mb=peak_rss / (1024 * 1024),
        rss_delta_mb=(peak_rss - baseline_rss) / (1024 * 1024),
        files_per_second=files_indexed / wall_time if wall_time else 0.0,
        megabytes_per_second=(total_bytes / (1024 * 1024)) / wall_time if wall_time else 0.0,
        files_indexed=files_indexed,
        chunks_stored=chunks_stored,
        database_size_mb=database_bytes / (1024 * 1024),
        idle_search_rss_mb=idle_search_rss_mb,
        cold_search_ms=cold_ms,
        warm_search_median_ms=warm_median_ms,
        warm_search_p95_ms=warm_p95_ms,
    )


def machine_metadata() -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor() or "unknown",
        "haydar_version": __version__,
        "gpu_used": False,
        "memory_sampler": "psutil RSS sampled every 20 ms" if HAS_PSUTIL else "tracemalloc Python heap",
    }
    if HAS_PSUTIL:
        metadata["installed_ram_gb"] = psutil.virtual_memory().total / (1024**3)
    return metadata


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark Haydar indexing and semantic first-result latency."
    )
    parser.add_argument("--files", type=int, default=5000, help="Generated file count (default: 5000).")
    parser.add_argument("--embedding-batch-size", type=int, default=None, help="Embedding batch size (default: config value).")
    parser.add_argument("--store", choices=("mocked", "real"), default="mocked", help="Use a no-op store or real ChromaDB embeddings.")
    parser.add_argument("--corpus", type=Path, help="Existing read-only corpus; disables synthetic generation.")
    parser.add_argument("--data-dir", type=Path, help="Isolated Haydar data directory (required for real mode).")
    parser.add_argument("--queries", type=Path, help="UTF-8 file with one semantic query per line.")
    parser.add_argument("--repeat", type=int, default=1, help="Number of clean runs (default: 1).")
    parser.add_argument("--seed", type=int, default=0, help="Synthetic corpus random seed.")
    parser.add_argument("--output", type=Path, help="Write complete results as JSON.")
    return parser


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.files <= 0:
        parser.error("--files must be greater than zero")
    if args.repeat <= 0:
        parser.error("--repeat must be greater than zero")
    if args.embedding_batch_size is not None and args.embedding_batch_size <= 0:
        parser.error("--embedding-batch-size must be greater than zero")
    if args.corpus is not None and not args.corpus.is_dir():
        parser.error(f"--corpus is not a directory: {args.corpus}")
    if args.queries is not None and not args.queries.is_file():
        parser.error(f"--queries is not a file: {args.queries}")
    if args.store == "real" and args.data_dir is None:
        parser.error("--data-dir is required with --store real")


def print_result(result: RunResult) -> None:
    print(f"Run {result.run}: {result.wall_time_seconds:.2f} s, "
          f"{result.files_per_second:.2f} files/s, peak RSS {result.peak_rss_mb:.2f} MB")
    if result.cold_search_ms is not None:
        print(f"  Search: cold {result.cold_search_ms:.2f} ms, "
              f"warm median {result.warm_search_median_ms:.2f} ms, "
              f"warm p95 {result.warm_search_p95_ms:.2f} ms")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)

    temporary_corpus: Path | None = None
    temporary_data: Path | None = None
    if args.corpus is None:
        temporary_corpus = Path(tempfile.mkdtemp(prefix="haydar_bench_corpus_"))
        corpus = temporary_corpus
        print(f"Generating {args.files} deterministic files in {corpus}...")
        generate_corpus(corpus, args.files, args.seed)
    else:
        corpus = args.corpus.resolve()

    if args.data_dir is None:
        temporary_data = Path(tempfile.mkdtemp(prefix="haydar_bench_data_"))
        data_dir = temporary_data
    else:
        data_dir = args.data_dir.resolve()
        if data_dir == corpus or data_dir in corpus.parents or corpus in data_dir.parents:
            parser.error("--data-dir and --corpus must not contain one another")
        data_dir.mkdir(parents=True, exist_ok=True)

    try:
        configure_data_dir(data_dir)
        file_count, total_bytes = corpus_stats(corpus)
        if file_count == 0:
            parser.error(f"Corpus contains no files: {corpus}")
        queries = load_queries(args.queries)
        config = HaydarConfig(folders=[str(corpus)], initialized=True)
        config.excluded_patterns = []
        if args.embedding_batch_size is not None:
            config.embedding_batch_size = args.embedding_batch_size

        print(f"Mode: {args.store}; corpus: {file_count} files, {total_bytes / (1024 * 1024):.2f} MB")
        results = [
            run_once(
                run_number=run_number,
                store_mode=args.store,
                corpus=corpus,
                total_bytes=total_bytes,
                config=config,
                queries=queries,
                data_dir=data_dir,
            )
            for run_number in range(1, args.repeat + 1)
        ]
        for result in results:
            print_result(result)

        payload = {
            "schema_version": 1,
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": args.store,
            "methodology": (
                "crawl/extract/chunk only; vector store mocked"
                if args.store == "mocked"
                else "end-to-end indexing with real embeddings and ChromaDB"
            ),
            "machine": machine_metadata(),
            "corpus": {
                "path": str(corpus) if args.corpus is not None else "generated temporary corpus",
                "generated": args.corpus is None,
                "seed": args.seed if args.corpus is None else None,
                "files": file_count,
                "bytes": total_bytes,
            },
            "configuration": {
                "embedding_model": config.embedding_model,
                "embedding_batch_size": config.embedding_batch_size,
                "chunk_size": config.chunk_size,
                "chunk_overlap": config.chunk_overlap,
                "repeat": args.repeat,
                "queries": queries if args.store == "real" else [],
            },
            "runs": [asdict(result) for result in results],
        }
        print(json.dumps(payload, indent=2))
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"Wrote benchmark results to {args.output}")
        return 0
    finally:
        if temporary_corpus is not None:
            shutil.rmtree(temporary_corpus, ignore_errors=True)
        if temporary_data is not None:
            shutil.rmtree(temporary_data, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
