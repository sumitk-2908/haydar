# Haydar

**Fast, local semantic file search for Windows.**

Haydar finds files by what they contain, not just by filename. It combines on-device AI embeddings with exact-text keyword search, keeps the index on your computer, and puts a floating search window a keystroke away. No file content is sent to a cloud service and there are no API costs.

Download `haydar.exe`, run it, and search. Setup happens on first launch.

## Features

- **Semantic search** finds related content by meaning, even when the query and document use different words.
- **Keyword search** uses ripgrep for fast exact-text matches.
- **Broad file support** covers PDF, DOCX, text, Markdown, source code, and optional image OCR.
- **Fully local operation** stores models, extracted content, and the vector index under your user profile.
- **Floating desktop UI** opens with `Ctrl+Space` and closes with `Esc`.
- **Live indexing** watches configured folders for new, changed, and deleted files.
- **Optional command line** supports automation and scripting workflows.

[Install Haydar](installation.md){ .md-button .md-button--primary }

[Follow the quick start](quick-start.md){ .md-button }

## What first launch does

1. Haydar selects your **Documents** folder. Everything else is opt-in, and larger locations are size-checked and confirmed before they are added.
2. It prepares search: the embedding model (about 80 MB, downloaded once) and keyword search.
3. **Search opens as soon as it is ready** — before indexing has finished.
4. Indexing continues in the background, and each committed batch becomes searchable immediately. Partial results improve as it progresses.
5. Indexing can be paused, cancelled, and resumed. After a failure or a restart it recovers automatically and keeps the work already committed.

## How It Works

1. Haydar crawls the folders you choose and excludes known build, cache, and system directories.
2. It extracts text, splits it into overlapping chunks, and creates local vector embeddings.
3. Semantic queries search ChromaDB while keyword queries search the configured folders with ripgrep.
4. The search window presents the best matching file, folder, snippet, and score.

!!! note "Windows application"
    Haydar targets 64-bit Windows 10 and Windows 11. Python is only required when installing from source.
