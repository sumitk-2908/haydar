"""
Haydar CLI -- powered by Typer.

An optional expert interface over the same services `haydar.exe` uses. It is not
required for first run, OCR installation, or recovery.

Commands:
    haydar init        Initialize Haydar, select folders, run first index
    haydar search      Search files (opens UI if no query, CLI if query given)
    haydar watch       Start background file watcher daemon
    haydar status      Show index statistics
    haydar config      Show or edit configuration
    haydar reindex     Force a full re-index of all folders
    haydar ocr         Install, inspect, and backfill image text recognition
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table

from haydar import __version__
from haydar.config import (
    DB_DIR,
    HAYDAR_DIR,
    HaydarConfig,
    _default_folders,
)
from haydar.ocr import (
    TesseractInfo,
    TesseractStatus,
    detect_tesseract,
    get_install_instructions,
)

logger = logging.getLogger(__name__)
console = Console()

app = typer.Typer(
    name="haydar",
    help="Haydar -- Find any file by what it contains, not what it's named.",
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=True,
)


def _banner() -> None:
    """Print the Haydar banner."""
    rprint(
        Panel.fit(
            "[bold cyan]Haydar[/bold cyan]  "
            f"[dim]v{__version__}[/dim]\n"
            "[dim]Fast, local semantic file search[/dim]",
            border_style="cyan",
        )
    )


# -- haydar init ----------------------------------------------------------------


@app.command()
def init(
    folders: list[str] | None = typer.Option(
        None,
        "--folders", "-f",
        help="Folders to index. If not specified, uses defaults with interactive confirmation.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip interactive prompts and accept defaults.",
    ),
) -> None:
    """Initialize Haydar -- select folders and run the first index."""
    _banner()

    config = HaydarConfig.load()

    if folders:
        # Validate provided folders
        validated = []
        for f in folders:
            p = Path(f).resolve()
            if p.is_dir():
                validated.append(str(p))
            else:
                _warning(f"Skipping '{f}' because it is not a valid directory.")
        for v in validated:
            if v not in config.folders:
                config.folders.append(v)
    else:
        # Default to the user's common document folders
        defaults = _default_folders()
        if not defaults:
            _fail(Exception("No default folders were found. Specify folders with `--folders`."))

        for d in defaults:
            if d not in config.folders:
                rprint(f"Adding [cyan]{d}[/cyan] to the Haydar index...")
                config.folders.append(d)

    if not config.folders:
        _fail(Exception("No folders are configured for indexing."))

    from haydar.lifecycle import IndexLifecycle

    lifecycle = IndexLifecycle(config)
    config.ensure_dirs()
    lifecycle.mark_folders_configured(config.folders)

    rprint(f"[green]>[/green] Config saved to [dim]{HAYDAR_DIR}[/dim]")
    rprint(f"[green]>[/green] Database at [dim]{DB_DIR}[/dim]\n")

    # Setup provisions search readiness; the crawl is a separate, resumable job.
    try:
        from haydar.setup import SetupCoordinator

        rprint("[dim]Downloading embedding model (~80 MB); first run only, this may take a minute...[/dim]")
        _print_init_ocr_status(detect_tesseract())

        with console.status("[cyan]Preparing search...") as status:
            def report(event) -> None:
                status.update(f"[cyan]{escape(event.message)}")

            SetupCoordinator(config).prepare_search(progress_callback=report)
        rprint("[green]+[/green] Search is ready.\n")
    except Exception as exc:
        if not getattr(exc, "hint", None):
            exc.hint = "Your configuration was saved. Run `haydar-cli.exe init` again to retry."
        _fail(exc)

    rprint("[bold]Starting initial index...[/bold]\n")
    try:
        from haydar.indexer.engine import IndexingEngine, JobControl, JobOutcome

        control = JobControl()
        with IndexingEngine(config, allow_download=True) as engine, Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("({task.completed}/{task.total})"),
            console=console,
        ) as progress:
            task = progress.add_task("[cyan]Indexing files...", total=None)

            def on_progress(completed: int, total: int) -> None:
                progress.update(task, completed=completed, total=total or None)

            lifecycle.transition("running")
            snapshot = engine.run_job(
                control=control, progress_callback=on_progress
            )

        if snapshot.outcome is JobOutcome.COMPLETE:
            lifecycle.transition("complete")
        elif snapshot.outcome is JobOutcome.FAILED:
            lifecycle.transition("failed", error=snapshot.error_message)
        elif snapshot.outcome is JobOutcome.CANCELLED:
            lifecycle.transition("cancelled")

        rprint("")
        _print_index_stats(snapshot.to_stats())
        if snapshot.ocr_deferred:
            rprint(
                f"\n[yellow]{snapshot.ocr_deferred} image(s) are waiting for OCR.[/yellow]"
            )
            rprint("[dim]Run [bold]haydar-cli.exe ocr install[/bold] to enable image search.[/dim]")
        rprint("\n[green bold]+ Haydar is ready![/green bold]")
        rprint("[dim]Launch [bold]haydar.exe[/bold] for the floating search window.[/dim]")
        rprint("[dim]Run [bold]haydar-cli.exe search 'query'[/bold] to search from here.[/dim]")
    except Exception as exc:
        if not getattr(exc, "hint", None):
            exc.hint = "Search is ready; indexing can be resumed by running this command again."
        _fail(exc)


# -- haydar search --------------------------------------------------------------


@app.command()
def search(
    query: str | None = typer.Argument(
        None,
        help="Search query. If omitted, opens the floating search UI.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit", "-n",
        help="Maximum number of results to display.",
    ),
    mode: str = typer.Option(
        "semantic",
        "--mode", "-m",
        help="Search mode: 'semantic' (meaning-based) or 'keyword' (ripgrep exact match).",
    ),
) -> None:
    """Search your files -- by content, semantically."""
    config = HaydarConfig.load()
    _check_ready(config)

    if limit is None:
        limit = config.results_limit

    if mode not in ("semantic", "keyword"):
        _fail(Exception(f"Search mode '{mode}' is invalid. Use 'semantic' or 'keyword'."))

    # A partial index is a valid state to search: results improve as the crawl
    # commits more batches.
    if config.initial_index_state not in ("complete",):
        rprint(
            f"[dim]Note: initial indexing is {config.initial_index_state}; "
            "results cover indexed files only.[/dim]"
        )

    if query is None:
        # Launch floating UI
        rprint("[dim]Launching search UI...[/dim]")
        try:
            from haydar.ui.window import launch_search_window

            launch_search_window(config)
        except ImportError as exc:
            exc.hint = "Run `haydar-cli.exe search 'your query'` for CLI search."
            _fail(exc)
    else:
        # CLI search
        try:
            from haydar.search.hybrid import HybridSearch

            searcher = HybridSearch(config)
            results = searcher.search(query, limit=limit, mode=mode)

            if not results:
                rprint(f"\n[yellow]No results found for '{query}'[/yellow]")
                raise typer.Exit(0)

            rprint(f"\n[bold]Results for:[/bold] [cyan]{query}[/cyan]\n")

            table = Table(show_header=True, header_style="bold cyan", show_lines=True)
            table.add_column("#", style="dim", width=3)
            table.add_column("File", style="bold")
            table.add_column("Path", style="dim")
            table.add_column("Snippet", max_width=60)
            table.add_column("Score", justify="right", style="green")

            for i, result in enumerate(results, 1):
                table.add_row(
                    str(i),
                    result.filename,
                    result.folder,
                    result.snippet[:120] + "..." if len(result.snippet) > 120 else result.snippet,
                    f"{result.score:.2f}",
                )

            console.print(table)

        except Exception as exc:
            _fail(exc)


# -- haydar watch ---------------------------------------------------------------


@app.command()
def watch(
    install_autostart: bool = typer.Option(
        False,
        "--install-autostart",
        help="Install Haydar watcher to run automatically on Windows startup.",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Wait for an in-progress initial index to finish instead of exiting.",
    ),
) -> None:
    """Start the background file watcher daemon."""
    config = HaydarConfig.load()
    _check_ready(config)

    if install_autostart:
        try:
            from haydar.indexer.watcher import install_autostart as _install

            _install()
            rprint("[green]+[/green] Autostart installed. Haydar watcher will run on login.")
        except Exception as exc:
            _fail(exc)
        return

    from haydar.lifecycle import IndexLifecycle

    lifecycle = IndexLifecycle(config)
    if not lifecycle.is_watcher_eligible:
        # The gate is never bypassed: starting now would race the initial crawl
        # for the writer lock.
        state = config.initial_index_state
        if not wait:
            rprint(
                f"[yellow]The initial index is {state}; the watcher cannot start yet.[/yellow]"
            )
            rprint(
                "[dim]Launch haydar.exe to finish indexing, or re-run with --wait.[/dim]"
            )
            raise typer.Exit(1)

        rprint(f"[dim]Initial index is {state}; waiting for it to finish...[/dim]")
        import time

        while not IndexLifecycle(HaydarConfig.load()).is_watcher_eligible:
            time.sleep(2.0)
        config = HaydarConfig.load()

    _banner()
    rprint("[bold]Starting file watcher...[/bold]")
    rprint(f"[dim]Watching {len(config.folders)} folder(s). Press Ctrl+C to stop.[/dim]\n")

    for folder in config.folders:
        rprint(f"  [cyan]*[/cyan]  {folder}")
    rprint("")

    try:
        from haydar.indexer.watcher import FileWatcher
        watcher = FileWatcher(config)
        watcher.start(blocking=True)
    except KeyboardInterrupt:
        rprint("\n[yellow]Watcher stopped.[/yellow]")
    except Exception as exc:
        _fail(exc)


# -- haydar status --------------------------------------------------------------


@app.command()
def status() -> None:
    """Show setup readiness, index progress, watcher eligibility, and OCR state."""
    config = HaydarConfig.load()
    _banner()

    from haydar.lifecycle import IndexLifecycle

    lifecycle = IndexLifecycle(config)

    rprint("[bold]Readiness:[/bold]")
    rprint(f"  [dim]Folders configured:[/dim]  {'yes' if config.folders_configured else 'no'}")
    rprint(f"  [dim]Search ready:[/dim]        {'yes' if config.search_ready else 'no'}")
    rprint(f"  [dim]Initial index:[/dim]       {config.initial_index_state}")
    if config.initial_index_error:
        rprint(f"  [dim]Last error:[/dim]          {escape(config.initial_index_error)}")
    rprint(
        f"  [dim]Watcher eligible:[/dim]    "
        f"{'yes' if lifecycle.is_watcher_eligible else 'no'}"
    )

    if not config.search_ready:
        rprint("\n[yellow]Search components are not ready yet.[/yellow]")
        rprint("[dim]Launch haydar.exe to finish setup.[/dim]")
        raise typer.Exit(0)

    try:
        from haydar.search.store import VectorStore

        store = VectorStore(config)
        stats = store.get_stats()
        rprint("")
        _print_index_stats(stats)
    except Exception as exc:
        _fail(exc)

    try:
        from haydar.indexer.cache import FileCache

        cache = FileCache()
        try:
            dispositions = cache.count_by_disposition()
        finally:
            cache.close()
        deferred = dispositions.get("ocr_deferred", 0)
        if deferred:
            rprint(f"\n[yellow]{deferred} image(s) are waiting for OCR.[/yellow]")
            rprint("[dim]Run `haydar-cli.exe ocr install` to enable image search.[/dim]")
    except Exception:
        logger.debug("Could not read cache dispositions", exc_info=True)

    info = detect_tesseract()
    rprint("\n[bold]OCR:[/bold]")
    if info.status is TesseractStatus.FOUND:
        rprint(f"  [green]ready[/green] ({escape(info.version or 'unknown')})")
    else:
        rprint(f"  [dim]{escape(info.status.value)}[/dim]")

    rprint("\n[bold]Config:[/bold]")
    rprint(f"  [dim]Model:[/dim]     {config.embedding_model}")
    rprint(f"  [dim]Hotkey:[/dim]    {config.hotkey}")
    rprint(f"  [dim]Debounce:[/dim]  {config.watcher_debounce_seconds}s")
    rprint("\n[bold]Folders:[/bold]")
    for folder in config.folders:
        rprint(f"  [cyan]>[/cyan]  {folder}")


# -- haydar config --------------------------------------------------------------


@app.command(name="config")
def show_config(
    add_folder: str | None = typer.Option(
        None,
        "--add-folder",
        help="Add a folder to the index list.",
    ),
    remove_folder: str | None = typer.Option(
        None,
        "--remove-folder",
        help="Remove a folder from the index list.",
    ),
    set_hotkey: str | None = typer.Option(
        None,
        "--set-hotkey",
        help="Set the global hotkey (pynput format, e.g., '<ctrl>+<space>').",
    ),
    set_model: str | None = typer.Option(
        None,
        "--set-model",
        help="Set the embedding model name.",
    ),
) -> None:
    """View or modify Haydar configuration."""
    config = HaydarConfig.load()

    changed = False

    if add_folder:
        p = Path(add_folder).resolve()
        if not p.is_dir():
            _fail(Exception(f"'{add_folder}' is not a valid directory."))
        folder_str = str(p)
        if folder_str not in config.folders:
            config.folders.append(folder_str)
            rprint(f"[green]+[/green] Added: {folder_str}")
            changed = True
        else:
            rprint(f"[yellow]Already indexed: {folder_str}[/yellow]")

    if remove_folder:
        p = Path(remove_folder).resolve()
        folder_str = str(p)
        if folder_str in config.folders:
            config.folders.remove(folder_str)
            rprint(f"[green]+[/green] Removed: {folder_str}")
            changed = True
        else:
            rprint(f"[yellow]Not in index: {folder_str}[/yellow]")

    if set_hotkey:
        config.hotkey = set_hotkey
        rprint(f"[green]+[/green] Hotkey set to: {set_hotkey}")
        changed = True

    if set_model:
        config.embedding_model = set_model
        rprint(f"[green]+[/green] Model set to: {set_model}")
        changed = True

    if changed:
        config.save()
        rprint("[dim]Config saved.[/dim]")
    else:
        # Just show current config
        _banner()
        rprint(f"[bold]Config file:[/bold] [dim]{config.__class__.__name__}[/dim]")
        rprint(f"  [dim]Embedding model:[/dim]  {config.embedding_model}")
        rprint(f"  [dim]Hotkey:[/dim]           {config.hotkey}")
        rprint(f"  [dim]Chunk size:[/dim]       {config.chunk_size} tokens")
        rprint(f"  [dim]Chunk overlap:[/dim]    {config.chunk_overlap} tokens")
        rprint(f"  [dim]Debounce:[/dim]         {config.watcher_debounce_seconds}s")
        rprint(f"\n[bold]Folders ({len(config.folders)}):[/bold]")
        for folder in config.folders:
            rprint(f"  [cyan]>[/cyan]  {folder}")
        rprint("\n[bold]Size limits:[/bold]")
        for category, limit in config.size_limits.items():
            rprint(f"  [dim]{category}:[/dim]  {limit // (1024 * 1024)} MB")
        rprint("\n[bold]Excluded patterns:[/bold]")
        rprint(f"  [dim]{', '.join(config.excluded_patterns[:8])}...[/dim]")


# -- haydar reindex -------------------------------------------------------------


@app.command()
def reindex(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Force a full re-index of all configured folders."""
    config = HaydarConfig.load()
    _check_ready(config)

    rprint("[bold]Starting full re-index...[/bold]\n")
    rprint("[yellow]! This will re-process all files. It may take a while.[/yellow]\n")

    if not yes and not typer.confirm("Continue?", default=True):
        raise typer.Exit(0)

    from haydar.lifecycle import IndexLifecycle

    lifecycle = IndexLifecycle(config)
    try:
        from haydar.indexer.engine import (
            IndexingEngine,
            JobControl,
            JobKind,
            JobOutcome,
        )

        with IndexingEngine(config, allow_download=True) as engine, Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("({task.completed}/{task.total})"),
            console=console,
        ) as progress:
            task = progress.add_task("[cyan]Re-indexing...", total=None)
            snapshot = engine.run_job(
                kind=JobKind.REBUILD,
                force=True,
                control=JobControl(),
                progress_callback=lambda done, total: progress.update(
                    task, completed=done, total=total or None
                ),
            )

        if snapshot.outcome is JobOutcome.COMPLETE:
            if lifecycle.state != "running":
                lifecycle.transition("running")
            lifecycle.transition("complete")
        elif snapshot.outcome is JobOutcome.FAILED:
            _fail(Exception(snapshot.error_message or "Re-index failed."))

        rprint("")
        _print_index_stats(snapshot.to_stats())
        rprint("\n[green bold]+ Re-index complete![/green bold]")
    except Exception as exc:
        _fail(exc)


# -- haydar ocr -----------------------------------------------------------------

ocr_app = typer.Typer(
    name="ocr",
    help="Install, inspect, and backfill image text recognition.",
    no_args_is_help=True,
)
app.add_typer(ocr_app)


def _render_ocr_readiness(info: TesseractInfo) -> None:
    """Print one line describing current OCR readiness."""
    version = escape(info.version or "unknown")
    path = escape(info.path or "unknown")
    if info.status is TesseractStatus.FOUND:
        rprint(f"[green]Ready:[/green] Tesseract {version} at {path}.")
    elif info.status is TesseractStatus.PYTHON_PACKAGE_MISSING:
        rprint("[red]The Python OCR adapter is missing from this build.[/red]")
        rprint(
            "  [dim]The engine itself is not the problem — reinstall Haydar to "
            "restore the bundled adapter.[/dim]"
        )
    elif info.status is TesseractStatus.NOT_FOUND:
        rprint("[yellow]Not installed:[/yellow] no text recognition engine is active.")
    elif info.status is TesseractStatus.WRONG_VERSION:
        rprint(f"[yellow]Tesseract {version} at {path} is older than v4.[/yellow]")
    else:
        rprint(f"[red]The engine at {path} could not be verified.[/red]")


@ocr_app.command("status")
def ocr_status_command() -> None:
    """Show text recognition readiness and how many images are waiting."""
    info = detect_tesseract()
    if info.detail:
        logger.warning("OCR readiness check failed: %s", info.detail)

    rprint("[bold]Text recognition:[/bold]")
    _render_ocr_readiness(info)

    from haydar.ocr import read_active_pointer

    pointer = read_active_pointer()
    if pointer:
        rprint(
            f"  [dim]Private install:[/dim] version {escape(str(pointer.get('version', '?')))}"
        )

    deferred = _deferred_image_count()
    if deferred:
        rprint(f"\n[yellow]{deferred} image(s) are waiting for text recognition.[/yellow]")

    if info.status is TesseractStatus.FOUND:
        if deferred:
            rprint("[dim]Run `haydar-cli.exe ocr backfill` to index them now.[/dim]")
    elif info.status is TesseractStatus.PYTHON_PACKAGE_MISSING:
        # Distinct from a missing engine, and the one state `ocr install` cannot
        # resolve: the adapter is checked before the binary is ever looked for,
        # so installing an engine changes nothing until the build is repaired.
        pass
    else:
        console.print(get_install_instructions(), markup=False)


@ocr_app.command("install")
def ocr_install_command(
    force: bool = typer.Option(
        False, "--force", help="Reinstall even when an engine is already active."
    ),
    backfill: bool = typer.Option(
        True,
        "--backfill/--no-backfill",
        help="Index images that were waiting for OCR once installation succeeds.",
    ),
) -> None:
    """Download, verify, and privately activate the text recognition engine."""
    from haydar.ocr import OcrProvisionError, install_ocr

    try:
        with console.status("[cyan]Setting up text recognition...") as status:

            def report(event) -> None:
                status.update(f"[cyan]{escape(event.message)}")

            result = install_ocr(progress_callback=report, force=force)
    except OcrProvisionError as exc:
        # Already phrased for a person; the error code is the diagnostic detail.
        logger.warning("OCR provisioning failed (%s)", exc.error_code)
        _fail(exc)
    except Exception as exc:
        _fail(exc)

    if not result.ready:
        _fail(Exception(result.message or "Text recognition setup did not finish."))

    rprint(f"[green]+[/green] {escape(result.message)}")
    if result.executable_path:
        rprint(f"[dim]Engine: {escape(result.executable_path)}[/dim]")

    if backfill:
        _run_ocr_backfill(result.version_token)


@ocr_app.command("backfill")
def ocr_backfill_command() -> None:
    """Index images that were deferred or read by an older engine."""
    info = detect_tesseract()
    if info.status is not TesseractStatus.FOUND:
        exc = Exception("Text recognition is not available yet.")
        exc.hint = "Run `haydar-cli.exe ocr install` first."
        _fail(exc)

    version_token = f"tesseract-{info.version}" if info.version else "tesseract"
    _run_ocr_backfill(version_token)


def _deferred_image_count() -> int:
    """How many images are recorded as waiting for OCR."""
    try:
        from haydar.indexer.cache import FileCache

        cache = FileCache()
        try:
            return cache.count_by_disposition().get("ocr_deferred", 0)
        finally:
            cache.close()
    except Exception:
        logger.debug("Could not read cache dispositions", exc_info=True)
        return 0


def _run_ocr_backfill(version_token: str) -> None:
    """Run the image-only backfill over the same coordinator the GUI uses."""
    config = HaydarConfig.load()
    _check_ready(config)

    from haydar.indexer.engine import JobOutcome
    from haydar.indexer.jobs import IndexJobCoordinator

    rprint("\n[bold]Adding image text to search...[/bold]")
    coordinator = IndexJobCoordinator(config)
    try:
        coordinator.start_ocr_backfill(version_token)
        coordinator.wait_for_terminal()
    except KeyboardInterrupt:
        coordinator.cancel()
        coordinator.wait_for_terminal(timeout=30)
        rprint("\n[yellow]Backfill cancelled. Indexed images remain searchable.[/yellow]")
        raise typer.Exit(1) from None
    except Exception as exc:
        _fail(exc)

    snapshot = coordinator.snapshot()
    if snapshot.outcome is JobOutcome.FAILED:
        _fail(Exception(snapshot.error_message or "The image backfill failed."))

    rprint(
        f"[green]+[/green] {snapshot.committed_files} image(s) indexed, "
        f"{snapshot.ocr_deferred} still deferred."
    )


# -- haydar ocr-status ----------------------------------------------------------


@app.command("ocr-status")
def ocr_status() -> None:
    """Show Tesseract OCR installation status and install instructions.

    Retained as an alias for `haydar-cli.exe ocr status`, which is the form the
    other commands point at.
    """
    info = detect_tesseract()
    if info.detail:
        logger.warning("OCR readiness check failed: %s", info.detail)

    version = escape(info.version or "unknown")
    path = escape(info.path or "unknown")
    if info.status is TesseractStatus.FOUND:
        rprint(f"[green]Tesseract {version} found at {path}; image OCR enabled.[/green]")
    elif info.status is TesseractStatus.PYTHON_PACKAGE_MISSING:
        rprint("[red]Haydar's Python OCR adapter is not installed; image OCR disabled.[/red]")
        console.print(get_install_instructions(), markup=False)
    elif info.status is TesseractStatus.NOT_FOUND:
        rprint("[red]Tesseract executable not found; image OCR disabled.[/red]")
        console.print(get_install_instructions(), markup=False)
    elif info.status is TesseractStatus.WRONG_VERSION:
        rprint(f"[yellow]Tesseract {version} found at {path}, but v4+ is required.[/yellow]")
        console.print(get_install_instructions(), markup=False)
    else:
        rprint(f"[red]Tesseract at {path} could not be verified; image OCR disabled.[/red]")
        rprint("[dim]Check the Haydar log, then run `haydar-cli.exe ocr status` again.[/dim]")


# -- haydar update-check --------------------------------------------------------


@app.command("update-check")
def update_check(
    force: bool = typer.Option(
        False,
        "--force",
        help="Force check even if interval has not elapsed.",
    ),
) -> None:
    """Check for a newer Haydar release on GitHub."""
    import time

    from haydar.updater import get_latest_version, get_release_url, is_newer

    config = HaydarConfig.load()
    now = time.time()

    if not force:
        if now < config.update_check_snoozed_until:
            rprint("Up to date (update notification dismissed temporarily).")
            return
        elapsed = now - config.last_update_check
        if 0 <= elapsed < config.update_check_interval_hours * 3600:
            rprint("Up to date (last checked recently).")
            return

    latest = get_latest_version()
    if latest is None:
        _fail(Exception("Could not reach GitHub."))

    if is_newer(latest, __version__):
        rprint(f"Update available: {latest}")
        rprint(f"Download: {get_release_url(latest)}")
    else:
        rprint(f"Up to date (current: {__version__}).")

    config.last_update_check = time.time()
    config.save()


# -- haydar uninstall -------------------------------------------------------------


@app.command()
def uninstall(
    remove_data: bool = typer.Option(
        False,
        "--remove-data",
        help="Also delete the index and configuration in ~/.haydar (backed up first).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be removed without removing anything.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip interactive confirmation.",
    )
) -> None:
    """Remove Haydar's autostart entry, and optionally its indexed data.

    Indexed data and configuration in ``~/.haydar`` are **preserved by default**,
    matching ``uninstall.ps1 -RemoveData``: reinstalling or upgrading must never
    silently discard a user's index, and the two uninstallers must not disagree
    about what "uninstall" means. Pass ``--remove-data`` to delete it.
    """
    import shutil
    import time

    startup_dir = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    bat_path = startup_dir / "haydar_watcher.bat"

    rprint("[bold]Uninstalling Haydar...[/bold]\n")

    if dry_run:
        rprint("[yellow]DRY RUN - No files will be removed.[/yellow]")
        if remove_data:
            state = "" if HAYDAR_DIR.exists() else " (Not found)"
            rprint(f"Would back up and delete: [cyan]{HAYDAR_DIR}[/cyan]{state}")
        else:
            rprint(f"Would keep: [cyan]{HAYDAR_DIR}[/cyan] (use --remove-data to delete)")

        state = "" if bat_path.exists() else " (Not found)"
        rprint(f"Would delete: [cyan]{bat_path}[/cyan]{state}")
        raise typer.Exit(0)

    if remove_data and not yes:
        confirm = typer.confirm("This will permanently delete your Haydar index and configuration. A backup will be saved to your Desktop. Continue?", default=False)
        if not confirm:
            raise typer.Exit(0)

    if remove_data and HAYDAR_DIR.exists():
        timestamp = int(time.time())
        desktop = Path.home() / "Desktop"
        backup_dir = desktop if desktop.exists() else Path.home()
        backup_path = backup_dir / f"haydar_backup_{timestamp}"

        rprint(f"Creating backup of {HAYDAR_DIR}...")
        try:
            shutil.make_archive(str(backup_path), 'zip', str(HAYDAR_DIR))
            rprint(f"[green]+[/green] Backup saved to: {backup_path}.zip")
        except Exception as e:
            rprint(f"[red]x[/red] Failed to create backup: {e}")
            rprint("Aborting uninstall to prevent data loss.")
            raise typer.Exit(1)

        try:
            shutil.rmtree(HAYDAR_DIR)
            rprint(f"[green]+[/green] Deleted {HAYDAR_DIR}")
        except Exception as e:
            rprint(f"[red]x[/red] Failed to delete {HAYDAR_DIR}: {e}")
    elif not remove_data:
        rprint(f"[green]+[/green] Kept your index and settings in {HAYDAR_DIR}")

    # Delete autostart
    if bat_path.exists():
        try:
            bat_path.unlink()
            rprint(f"[green]+[/green] Removed autostart script {bat_path}")
        except Exception as e:
            rprint(f"[red]x[/red] Failed to remove autostart script: {e}")

    rprint("\n[green bold]+ Haydar uninstalled successfully.[/green bold]")


# -- haydar version -------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        rprint(f"Haydar v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version", "-v",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Haydar -- Find any file by what it contains, not what it's named."""
    from haydar.logging_setup import setup_logging

    setup_logging()

# -- Helpers --------------------------------------------------------------------


def _print_init_ocr_status(info: TesseractInfo) -> None:
    """Render a concise, markup-safe OCR readiness notice during initialization."""
    if info.detail:
        logger.warning("OCR readiness check failed: %s", info.detail)

    version = escape(info.version or "unknown")
    if info.status is TesseractStatus.FOUND:
        rprint(f"[green]✓ Tesseract {version} found — image OCR enabled.[/green]")
    elif info.status is TesseractStatus.PYTHON_PACKAGE_MISSING:
        # Never a pip instruction: the adapter ships inside the application, so
        # its absence is a build problem the user cannot fix with a package
        # manager.
        rprint("[yellow]This build's OCR adapter is missing. Image OCR disabled.[/yellow]")
        rprint("[dim]Reinstall Haydar to restore image text search.[/dim]")
    elif info.status is TesseractStatus.NOT_FOUND:
        rprint("[yellow]Tesseract executable not found. Image OCR disabled.[/yellow]")
        rprint("[dim]Run `haydar-cli.exe ocr install` to set it up.[/dim]")
    elif info.status is TesseractStatus.WRONG_VERSION:
        rprint(f"[yellow]⚠ Tesseract {version} found but v4+ required. Image OCR disabled.[/yellow]")
        rprint("[dim]Run `haydar-cli.exe ocr install` to set up a supported engine.[/dim]")
    else:
        rprint("[yellow]Tesseract could not be verified. Image OCR disabled; check the Haydar log.[/yellow]")


def launch_ui() -> None:
    """Entry point for the `haydar-ui` gui-script (windowed, no console)."""
    from haydar.gui_main import main

    main()


def _ensure_ripgrep() -> None:
    """Ensure ripgrep is available for keyword search; fetch it if missing.

    Failure is non-fatal: keyword search will be unavailable but semantic
    search still works, so we warn rather than abort init.
    """
    from haydar.config import RIPGREP_DIR, HaydarConfigError, get_rg_path

    try:
        get_rg_path()
        return
    except HaydarConfigError:
        pass

    rprint("[dim]Fetching ripgrep (for keyword search)...[/dim]")
    try:
        from haydar.ripgrep import ensure_ripgrep

        path = ensure_ripgrep(RIPGREP_DIR)
        rprint(f"[green]>[/green] ripgrep ready at [dim]{path}[/dim]")
    except Exception as exc:
        _warning(
            f"Could not fetch ripgrep: {exc}. Keyword search will be unavailable; "
            "semantic search still works."
        )


def _fail(exc: Exception) -> None:
    """Print a friendly error (with hint if present) and raise typer.Exit(1).

    Re-raises typer.Exit unchanged so a deliberate clean exit (e.g. exit code 0
    for 'no results') inside a try/except is not turned into a failure.
    """
    from haydar.config import get_log_path

    if isinstance(exc, typer.Exit):
        raise exc

    rprint(f"[red]x {escape(str(exc))}[/red]")
    hint = getattr(exc, "hint", None)
    if hint:
        rprint(f"[dim]{escape(str(hint))}[/dim]")

    log_msg = f"Full log: {get_log_path()}"
    normalized_path = os.path.normcase(str(get_log_path())).replace("\\", "/")
    rendered = f"{exc}\n{hint or ''}".replace("\\", "/").lower()
    if normalized_path.lower() not in rendered:
        rprint(f"[dim]{escape(log_msg)}[/dim]")

    raise typer.Exit(1)


def _warning(message: str) -> None:
    """Render a safe nonfatal warning with the canonical diagnostic path."""
    from haydar.config import get_log_path

    rprint(f"[yellow]! {escape(message)}[/yellow]")
    rprint(f"[dim]{escape(f'Full log: {get_log_path()}')}[/dim]")


def _check_ready(config: HaydarConfig) -> None:
    """Abort if search prerequisites are unavailable, or the schema is outdated.

    Gating is on ``search_ready``, not on the legacy ``initialized`` flag and not
    on crawl completion: a partial index is a valid, searchable state.
    """
    if not config.search_ready:
        exc = Exception("Haydar's search components are not ready yet.")
        exc.hint = (
            "Launch haydar.exe to finish setup, or run `haydar-cli.exe init` for "
            "an interactive setup."
        )
        _fail(exc)

    from haydar.config import CURRENT_SCHEMA_VERSION
    if config.schema_version > CURRENT_SCHEMA_VERSION:
        exc = Exception(
            f"The index schema v{config.schema_version} is newer than this Haydar version supports."
        )
        exc.hint = "Install the newer Haydar version that created this index; do not reindex with this version."
        _fail(exc)
    if config.schema_version < CURRENT_SCHEMA_VERSION:
        exc = Exception(
            f"Database schema update required (v{config.schema_version} to v{CURRENT_SCHEMA_VERSION})."
        )
        exc.hint = "Run `haydar-cli.exe reindex` to update the database; your files are unaffected."
        _fail(exc)


# Retained under its historical name for callers and tests that predate the
# explicit lifecycle fields.
_check_initialized = _check_ready


def _print_index_stats(stats: dict) -> None:
    """Pretty-print indexing statistics."""
    table = Table(title="Index Statistics", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right", style="green")

    table.add_row("Files indexed", str(stats.get("files_indexed", 0)))
    table.add_row("Chunks stored", str(stats.get("chunks_stored", 0)))
    table.add_row("Files skipped (size)", str(stats.get("files_skipped_size", 0)))
    table.add_row("Files skipped (error)", str(stats.get("files_skipped_error", 0)))
    table.add_row("Total text extracted", _human_size(stats.get("total_text_bytes", 0)))

    if stats.get("db_size_bytes"):
        table.add_row("Database size", _human_size(stats["db_size_bytes"]))

    console.print(table)


def _human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} TB"
