# Quick Start

## 1. Download and Launch

Download `haydar.exe` from the [latest release](https://github.com/sumitk-2908/haydar/releases/latest), [verify it](installation.md#download-and-run), and run it.

There is no setup command. Haydar prepares itself on first launch:

- It selects your **Documents** folder to index. Nothing else is added without your approval.
- It downloads the embedding model (about 80 MB, once) and prepares keyword search.
- The search window opens as soon as search works — you do not wait for indexing.

!!! tip "Add more folders"
    Open **Settings** to add folders. Large locations such as Desktop, Downloads, whole drives, and network shares are size-checked first, and Haydar warns you before adding one that would take a long time to index.

## 2. Search While It Indexes

Indexing continues in the background after the window opens. This is normal, and search is fully usable throughout:

- Each batch of files becomes searchable the moment it is saved, so results improve as you work.
- The status band shows progress and offers **Pause** and **Cancel** at any time.
- Paused or cancelled, everything already indexed stays searchable. **Resume** picks up where it stopped.
- If indexing fails, or Windows restarts mid-index, Haydar recovers on the next launch and keeps the work it had committed. Finished files are not indexed twice.

When the initial crawl finishes, the band reports completion and collapses. From then on the file watcher keeps the index current automatically.

## 3. Open the Floating Search

Press:

```text
Ctrl+Space
```

The Haydar search window appears on the monitor containing the pointer, with focus in the query field. Press `Esc` to hide it.

## 4. Search

Type a concept rather than only a filename, for example:

```text
quarterly budget projections
```

Semantic mode returns files related to the meaning of the query. Toggle to keyword mode when you need an exact text match.

## 5. Open a Result

Use the arrow keys to select a result and press `Enter` to open it with the Windows default application. Press `Esc` to hide the search window.

## Optional: Image Text Search

Automatic setup of the text-recognition engine is **not available in this build** — no engine distribution currently meets Haydar's licensing and verification bar. See [KNOWN_GAPS.md](https://github.com/sumitk-2908/haydar/blob/master/KNOWN_GAPS.md). Haydar will, however, use an engine you install yourself.

Until you do, images are remembered as they are found, so nothing is lost.

### Enabling it

1. Install **Tesseract OCR for Windows, v4 or newer, including English language data**, using the installer linked from the Tesseract project's own documentation. Accept the default location and leave English selected. Tesseract is the only thing you need; Haydar bundles the rest.
2. Restart Haydar. It checks the standard locations under `Program Files` and `%LOCALAPPDATA%\Programs` automatically, so there is nothing to configure.
3. Images found before then stay queued, so the next indexing pass reads them — nothing is lost and no reindex is needed. To pick them up straight away, use the **Install OCR** action shown while images are waiting; with the engine already installed it skips any download and starts that catch-up immediately.

Images found after that are recognized automatically. Recognition runs entirely on your computer and your images are never uploaded.

`haydar-cli.exe ocr status` reports what is still missing if image results do not appear.

## Optional: The Command Line

`haydar-cli.exe` is an expert interface for automation and scripting, over the same engine as the application. It is never required — not for setup, OCR, recovery, or anything else.

```powershell
haydar-cli.exe status
haydar-cli.exe search "quarterly budget projections"
haydar-cli.exe search "TODO" --mode keyword --limit 20
haydar-cli.exe reindex
haydar-cli.exe config
haydar-cli.exe update-check
haydar-cli.exe ocr status
```

- `status` shows readiness, indexing state, and indexed file and chunk counts.
- `search` queries from a terminal; a result shows the filename, folder, snippet, and score.
- `reindex` rebuilds the index after model, chunking, or exclusion changes.
- `config` displays or updates configuration.
- `update-check` checks GitHub Releases for a newer version.
- `ocr status` reports whether image text search is available.

## Troubleshooting

- **Setup could not download the model:** Reconnect to the internet and launch Haydar again. It retries automatically and keeps everything it already verified.
- **Indexing stopped:** The status band explains why and offers **Retry** or **Resume**. Search continues to cover the files already indexed.
- **Searches return nothing:** Confirm your folders in Settings. If the index itself looks wrong, use **Rebuild index** there.
- **`Ctrl+Space` does not work:** Another application may have reserved the hotkey. Change it in Settings.
- Full diagnostic logs are stored at `~/.haydar/logs/haydar.log`.
