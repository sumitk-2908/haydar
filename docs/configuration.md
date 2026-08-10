# Configuration

Haydar stores user configuration in `~/.haydar/config.json`. The Settings window is the easiest way to edit common values, and is all a normal user needs. You can also edit the JSON directly while Haydar is stopped, or use `haydar-cli.exe config` for scripting.

!!! warning
    Make a backup before manually editing JSON. Haydar preserves unknown keys, but invalid JSON causes it to fall back to defaults (the unreadable file is kept alongside as `config.json.corrupt-<timestamp>`). After changing the embedding model or chunk settings, rebuild the index from the Settings window.

## Configuration Fields

| Field | Type | Default | Description |
|---|---|---:|---|
| `folders` | `list[str]` | Documents | Absolute folders Haydar crawls and indexes. First launch selects Documents only; add more in Settings. |
| `excluded_patterns` | `list[str]` | Built-in exclusion list | Exact directory/file names or suffix globs such as `*.egg-info` that are skipped during crawling. |
| `size_limits` | `dict[str, int]` | Text 10 MB, document 100 MB, image 20 MB | Per-category file-size limits in bytes. Categories are `text`, `document`, and `image`. |
| `embedding_model` | `str` | `all-MiniLM-L6-v2` | Sentence-transformers model used for semantic embeddings. Changing it requires a full reindex. |
| `chunk_size` | `int` | `500` | Approximate words per extracted text chunk. Changing it requires a full reindex. |
| `chunk_overlap` | `int` | `50` | Approximate overlapping words between adjacent chunks. Must be less than `chunk_size`. |
| `embedding_batch_size` | `int` | `1000` | Number of chunks processed per embedding batch. Lower it if indexing needs less memory. |
| `hotkey` | `str` | `<ctrl>+<space>` | Global search-window hotkey in pynput format. |
| `watcher_debounce_seconds` | `float` | `0.5` | Delay used to combine rapid file-system events before indexing. |
| `results_limit` | `int` | `10` | Maximum results shown by the CLI and GUI search. |
| `window_opacity` | `int` | `92` | Search-window opacity percentage, from 50 to 100. |
| `always_on_top` | `bool` | `true` | Keeps the floating search window above other windows. |
| `last_update_check` | `float` | `0.0` | Unix timestamp of the last completed release check. Managed automatically. |
| `update_check_interval_hours` | `float` | `24.0` | Minimum interval between automatic release checks. Set to `0` to opt out. |
| `update_check_snoozed_until` | `float` | `0.0` | Unix timestamp until which dismissed update notifications stay hidden. Managed automatically. |
| `last_seen_version` | `str` | `""` | Version used to determine whether the What's New notice should appear. Managed automatically. |
| `initialized` | `bool` | `false` | Legacy compatibility mirror of `search_ready`, kept so an older Haydar can still read this file. Managed automatically; never edit it. |
| `schema_version` | `int` | `1` | Local index schema version. Do not edit unless instructed by a release migration guide. |

### Lifecycle fields

These record how far first-run setup and the initial crawl progressed. Haydar manages all of them; they are documented so the file is readable, not so it can be hand-edited.

| Field | Type | Default | Description |
|---|---|---:|---|
| `config_format_version` | `int` | `2` | Shape of this file, independent of `schema_version`. A file written by a newer Haydar is refused rather than rewritten. |
| `folders_configured` | `bool` | `false` | Whether a folder selection has been persisted. |
| `search_ready` | `bool` | `false` | Whether search prerequisites are verified. This is what decides whether the search window opens; it does **not** mean indexing has finished. |
| `initial_index_state` | `str` | `not_started` | One of `not_started`, `running`, `paused`, `cancelled`, `failed`, `complete`. |
| `initial_index_error` | `str` | `""` | Reason the last crawl stopped, when it failed. |
| `initial_index_pause_reason` | `str` | `""` | `user` or `interrupted`. A user pause waits for an explicit Resume; an interrupted run auto-resumes on the next launch. |

## Example

A minimal manually edited configuration might look like this:

```json
{
  "folders": [
    "C:\\Users\\Alice\\Documents",
    "C:\\Projects"
  ],
  "excluded_patterns": [
    "node_modules",
    ".git",
    "build",
    "*.egg-info"
  ],
  "embedding_model": "all-MiniLM-L6-v2",
  "chunk_size": 500,
  "chunk_overlap": 50,
  "results_limit": 10,
  "hotkey": "<ctrl>+<space>",
  "initialized": true,
  "schema_version": 1
}
```

Fields omitted from older configuration files receive their current defaults.

## Configuration Workflow

1. Close Haydar if it is running.
2. Open the Settings window, or edit `~/.haydar/config.json`.
3. Save valid values and confirm the configured folders exist.
4. Rebuild the index from Settings after changing `embedding_model`, `chunk_size`, or `chunk_overlap`.
5. Relaunch Haydar. The file watcher restarts itself against the new folder set once the crawl reaches a safe state.

Runtime data is kept next to the configuration:

```text
~/.haydar/
├── bin/       verified ripgrep binary
├── cache/     extraction and file metadata cache
├── config.json
├── db/        local ChromaDB index
├── logs/      haydar.log
└── models/    downloaded embedding models
```
