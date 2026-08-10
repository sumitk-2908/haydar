# Performance

Haydar includes a reproducible benchmark harness for indexing throughput, memory,
database size, and semantic first-result latency. This page intentionally does
not publish an unverified performance claim: run the real-store matrix on the
release hardware and commit its generated JSON before replacing the pending
cells below.

## Benchmark modes

[`scripts/bench_index.py`](../scripts/bench_index.py) has two explicit modes:

- **Mocked store** (the default) measures filesystem crawl, extraction, chunking,
  and batching. It excludes embeddings and ChromaDB writes, so its output must
  not support an end-to-end indexing or search claim.
- **Real store** uses the configured sentence-transformers model and persistent
  ChromaDB. It measures clean indexing runs, resulting database size, and one
  cold plus 30 warm semantic first-result queries.

Peak RSS is sampled every 20 ms with `psutil` when available. If `psutil` is not
installed, the script labels the value as Python heap measured by `tracemalloc`;
that fallback is not comparable to process RSS.

## Required release environment

Record these values with every published run:

| Property | Value |
|----------|-------|
| Haydar version/commit | Pending real-hardware run |
| Windows version | Pending real-hardware run |
| Python version | Pending real-hardware run |
| CPU | Pending real-hardware run |
| Installed RAM | Pending real-hardware run |
| GPU used | No, unless explicitly recorded otherwise |
| Embedding model | `all-MiniLM-L6-v2` unless explicitly changed |
| Power mode and background workload | Pending real-hardware run |

Use a fixed, representative and non-sensitive corpus. Record its file-type mix,
total bytes, and the exact query file. Do not compare runs made from different
corpora as though corpus size were the only variable.

## Reproduction

Install the project and optional RSS sampler in a clean environment:

```powershell
pip install -e .[dev,ocr]
pip install psutil
```

Run the pipeline-only matrix for 10,000 generated files. Repeat each command for
batch sizes `100`, `250`, `500`, `1000`, and `2000`:

```powershell
python scripts/bench_index.py --store mocked --files 10000 `
  --embedding-batch-size 100 --repeat 3 `
  --output benchmark-mocked-100.json
```

Run the publishable end-to-end matrix against a read-only corpus. The data
directory must be dedicated to benchmarking; each repeat clears its database and
file cache but retains the downloaded model:

```powershell
python scripts/bench_index.py --store real `
  --corpus C:\benchmarks\haydar-corpus-10000 `
  --data-dir C:\benchmarks\haydar-data `
  --queries C:\benchmarks\queries.txt `
  --embedding-batch-size 1000 --repeat 3 `
  --output benchmark-real-10000.json
```

For the required corpus-size matrix, use fixed 1,000-, 5,000-, and 10,000-file
subsets and run each configuration at least three times. The script emits a
machine-readable JSON record containing machine metadata, corpus bytes, Haydar
configuration, methodology, and every raw run. Publish medians from the raw runs;
retain the JSON as release evidence.

!!! warning "Isolated data directory"
    Never point `--data-dir` at a normal `~/.haydar` directory. Real mode deletes
    the benchmark directory's `db` and `cache` children between repeats. The
    script rejects a data directory that contains, or is contained by, the
    corpus. A user-provided corpus is never modified or deleted.

## Indexing results

These tables remain pending until the controlled W3-2 real-hardware run is
performed. Historical mocked-store values were removed because they did not
record the required corpus or machine and could not substantiate product claims.

### Mocked pipeline, 10,000 files

| Batch size | Median wall time (s) | Peak RSS (MB) | Files/s | MB/s |
|------------|----------------------|---------------|---------|------|
| 100 | Pending | Pending | Pending | Pending |
| 250 | Pending | Pending | Pending | Pending |
| 500 | Pending | Pending | Pending | Pending |
| 1000 | Pending | Pending | Pending | Pending |
| 2000 | Pending | Pending | Pending | Pending |

### Real end-to-end indexing

| Corpus | Median wall time (s) | Files/min | Peak RSS (MB) | Index size (MB) |
|--------|----------------------|-----------|---------------|-----------------|
| 1,000 files | Pending | Pending | Pending | Pending |
| 5,000 files | Pending | Pending | Pending | Pending |
| 10,000 files | Pending | Pending | Pending | Pending |

## Semantic search latency

Latency is measured inside the process from calling semantic search until the
first non-empty result batch. The first query is reported as cold. The harness
then reports median and p95 across 30 warm queries. This excludes UI typing and
debounce time and fails the run if any benchmark query returns no result.

| Corpus | Cold first result (ms) | Warm median (ms) | Warm p95 (ms) | Idle search RSS (MB) | Queries |
|--------|-------------------------|------------------|---------------|----------------------|---------|
| 1,000 files | Pending | Pending | Pending | Pending | Pending query file |
| 5,000 files | Pending | Pending | Pending | Pending | Pending query file |
| 10,000 files | Pending | Pending | Pending | Pending | Pending query file |

## Publication rule

Do not use the mocked pipeline table to claim sub-second search, embedding
throughput, or end-to-end memory usage. Update the README's performance statement
only after the real 10,000-file row is populated from committed benchmark JSON.
The statement must name the measured latency statistic, corpus size, CPU, RAM,
and link back to this methodology.
