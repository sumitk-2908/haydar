# Architecture

Haydar is a local desktop application with a layered Python design. The CLI is the composition root; the UI talks to the search interface and configuration without depending on indexer internals.

## Layers

```text
cli.py
├── ui/ (SearchWindow, SettingsWindow)
│   ├── search/ (HybridSearch, VectorStore interface)
│   └── config.py
├── search/ (HybridSearch, VectorStore)
│   └── config.py
└── indexer/ (IndexingEngine, FileWatcher, FileCache, extractors)
    └── config.py
```

The dependency direction is:

```text
config <- search <- indexer
config/search <- ui
cli <- everything
```

The UI layer deliberately does not import `src/haydar/indexer/` implementation modules. This keeps the desktop frontend replaceable and prevents UI code from taking ownership of crawling or storage details.

## Search Flow

### Semantic Search

1. `SearchWindow` or the `search` CLI command creates `HybridSearch`.
2. `HybridSearch` lazily creates `VectorStore`, so constructing the search UI does not load the embedding model.
3. The query is embedded locally by the configured sentence-transformers model.
4. ChromaDB returns nearby chunks by vector distance.
5. `HybridSearch` merges chunk hits, keeps the best hit per file, formats plain-text snippets, and applies the result limit.

### Keyword Search

1. `HybridSearch` resolves the verified ripgrep binary through `get_rg_path()`.
2. It starts ripgrep with JSON output, case-insensitive matching, configured folders, and a bounded result count.
3. Each JSON line is parsed by the pure `_parse_rg_line()` function.
4. Malformed, non-match, or incomplete events are ignored without aborting the search.
5. The GUI streams results while ripgrep is running and terminates the process when a search is cancelled.

## Indexing Pipeline

```text
crawl configured folders
        |
        v
exclude system/build/cache paths
        |
        v
check extension, size, mtime, and cache
        |
        v
extract text (PDF, DOCX, text, code, optional OCR)
        |
        v
chunk extracted text with overlap
        |
        v
embed chunks in batches
        |
        v
upsert vectors and metadata into ChromaDB
```

`IndexingEngine` uses a file cache containing path, modification time, size, and hash information. Unchanged files are skipped. Deleted files are removed from the vector store during a full crawl. A process-exclusive indexing lock prevents a full index and watcher updates from writing to the database at the same time.

## Ripgrep Provisioning

Keyword search executes a ripgrep binary, so it is pinned and verified rather than trusted. Haydar checks the archive or binary against a hardcoded SHA-256 before it is used; a missing or mismatched checksum is a hard failure, never a warning.

The binary is bundled into the release executables, so a packaged first run needs no download for keyword search. A source or editable install provisions it into `~/.haydar/bin/` with `python scripts/pull-rg.py`, or on first launch if it is absent. Setup re-probes whichever binary it resolves — the one keyword search will actually execute — with a bounded `rg --version` call.

```text
first launch (verifying_keyword_search)
    |
    v
resolve bundled or user ripgrep path
    |
    +--> missing: download release archive
    |        |
    |        v
    |    verify pinned SHA-256
    |        |
    |        v
    |    install ~/.haydar/bin/rg.exe
    |
    v
keyword search executes only the verified binary
```

## Runtime Data Layout

```text
~/.haydar/
├── .indexing.lock       process lock marker
├── bin/rg.exe           verified ripgrep
├── cache/               extraction and file metadata caches
├── config.json          HaydarConfig JSON
├── db/chroma.sqlite3    local vector database
├── logs/haydar.log      diagnostic log
└── models/              local embedding model files
```

All indexed content, embeddings, and operational logs remain on the local machine. Network access is used for first-time dependency/model provisioning and optional release-version checks, not for query processing or storage.
