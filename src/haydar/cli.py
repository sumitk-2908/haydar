"""
Haydar CLI -- powered by Typer.

Commands:
    haydar init        Initialize Haydar, select folders, run first index
    haydar search      Search files (opens UI if no query, CLI if query given)
    haydar watch       Start background file watcher daemon
    haydar status      Show index statistics
    haydar config      Show or edit configuration
    haydar reindex     Force a full re-index of all folders
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from haydar import __version__
from haydar.config import (
    HaydarConfig,
    _default_folders,
    HAYDAR_DIR,
    DB_DIR,
    ALL_INDEXABLE_EXTENSIONS,
)

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
            "[dim]v{}[/dim]\n"
            "[dim]Fast, local semantic file search[/dim]".format(__version__),
            border_style="cyan",
        )
    )


# -- haydar init ----------------------------------------------------------------


@app.command()
def init(
    folders: Optional[list[str]] = typer.Option(
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
                rprint(f"[yellow]! Skipping '{f}' -- not a valid directory[/yellow]")
        config.folders = validated
    else:
        # Interactive: show defaults, ask for confirmation
        defaults = _default_folders()
        if not defaults:
            rprint("[yellow]No default folders found. Please specify with --folders.[/yellow]")
            raise typer.Exit(1)

        rprint("\n[bold]Folders to index:[/bold]")
        for i, folder in enumerate(defaults, 1):
            rprint(f"  [cyan]{i}.[/cyan] {folder}")

        if not yes:
            rprint("")
            confirm = typer.confirm(
                "Index these folders?",
                default=True,
            )
            if not confirm:
                rprint("\n[dim]Run [bold]haydar init --folders PATH[/bold] to specify custom folders.[/dim]")
                raise typer.Exit(0)

        config.folders = defaults

    if not config.folders:
        rprint("[red]x No folders to index. Aborting.[/red]")
        raise typer.Exit(1)

    # Save config
    config.initialized = True
    config.ensure_dirs()
    config.save()

    rprint("[green]>[/green] Config saved to [dim]{0}[/dim]".format(HAYDAR_DIR))
    rprint("[green]>[/green] Database at [dim]{0}[/dim]\n".format(DB_DIR))

    # Run initial index
    rprint("[bold]Starting initial index...[/bold]\n")
    try:
        from haydar.indexer.engine import IndexingEngine

        engine = IndexingEngine(config)
        stats = engine.index_all()

        rprint("")
        _print_index_stats(stats)
        rprint("\n[green bold]+ Haydar is ready![/green bold]")
        rprint("[dim]Run [bold]haydar search[/bold] to start searching.[/dim]")
        rprint("[dim]Run [bold]haydar watch[/bold] to start the file watcher.[/dim]")
    except Exception as exc:
        rprint(f"\n[red]x Indexing failed: {exc}[/red]")
        rprint("[dim]Your config has been saved. Run [bold]haydar init[/bold] again to retry.[/dim]")
        raise typer.Exit(1)


# -- haydar search --------------------------------------------------------------


@app.command()
def search(
    query: Optional[str] = typer.Argument(
        None,
        help="Search query. If omitted, opens the floating search UI.",
    ),
    limit: int = typer.Option(
        10,
        "--limit", "-n",
        help="Maximum number of results to display.",
    ),
) -> None:
    """Search your files -- by content, semantically."""
    config = HaydarConfig.load()
    _check_initialized(config)

    if query is None:
        # Launch floating UI
        rprint("[dim]Launching search UI...[/dim]")
        try:
            from haydar.ui.window import launch_search_window

            launch_search_window(config)
        except ImportError as exc:
            rprint(f"[red]x UI dependencies missing: {exc}[/red]")
            rprint("[dim]Run [bold]haydar search 'your query'[/bold] for CLI search.[/dim]")
            raise typer.Exit(1)
    else:
        # CLI search
        try:
            from haydar.search.hybrid import HybridSearch

            searcher = HybridSearch(config)
            results = searcher.search(query, limit=limit)

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
            rprint(f"[red]x Search failed: {exc}[/red]")
            raise typer.Exit(1)


# -- haydar watch ---------------------------------------------------------------


@app.command()
def watch(
    install_autostart: bool = typer.Option(
        False,
        "--install-autostart",
        help="Install Haydar watcher to run automatically on Windows startup.",
    ),
) -> None:
    """Start the background file watcher daemon."""
    config = HaydarConfig.load()
    _check_initialized(config)

    if install_autostart:
        try:
            from haydar.indexer.watcher import install_autostart as _install

            _install()
            rprint("[green]+[/green] Autostart installed. Haydar watcher will run on login.")
        except Exception as exc:
            rprint(f"[red]x Failed to install autostart: {exc}[/red]")
            raise typer.Exit(1)
        return

    _banner()
    rprint("[bold]Starting file watcher...[/bold]")
    rprint(f"[dim]Watching {len(config.folders)} folder(s). Press Ctrl+C to stop.[/dim]\n")

    for folder in config.folders:
        rprint(f"  [cyan]*[/cyan]  {folder}")
    rprint("")

    try:
        from haydar.indexer.watcher import FileWatcher

        watcher = FileWatcher(config)
        watcher.start()  # Blocks until Ctrl+C
    except KeyboardInterrupt:
        rprint("\n[yellow]Watcher stopped.[/yellow]")
    except Exception as exc:
        rprint(f"[red]x Watcher failed: {exc}[/red]")
        raise typer.Exit(1)


# -- haydar status --------------------------------------------------------------


@app.command()
def status() -> None:
    """Show index statistics and configuration."""
    config = HaydarConfig.load()
    _check_initialized(config)

    _banner()

    try:
        from haydar.search.store import VectorStore

        store = VectorStore(config)
        stats = store.get_stats()
        _print_index_stats(stats)
    except Exception as exc:
        rprint(f"[red]x Could not read index: {exc}[/red]")
        raise typer.Exit(1)

    rprint(f"\n[bold]Config:[/bold]")
    rprint(f"  [dim]Model:[/dim]     {config.embedding_model}")
    rprint(f"  [dim]Hotkey:[/dim]    {config.hotkey}")
    rprint(f"  [dim]Debounce:[/dim]  {config.watcher_debounce_seconds}s")
    rprint(f"\n[bold]Folders:[/bold]")
    for folder in config.folders:
        rprint(f"  [cyan]>[/cyan]  {folder}")


# -- haydar config --------------------------------------------------------------


@app.command(name="config")
def show_config(
    add_folder: Optional[str] = typer.Option(
        None,
        "--add-folder",
        help="Add a folder to the index list.",
    ),
    remove_folder: Optional[str] = typer.Option(
        None,
        "--remove-folder",
        help="Remove a folder from the index list.",
    ),
    set_hotkey: Optional[str] = typer.Option(
        None,
        "--set-hotkey",
        help="Set the global hotkey (pynput format, e.g., '<ctrl>+<space>').",
    ),
    set_model: Optional[str] = typer.Option(
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
            rprint(f"[red]x '{add_folder}' is not a valid directory.[/red]")
            raise typer.Exit(1)
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
        rprint(f"\n[bold]Size limits:[/bold]")
        for category, limit in config.size_limits.items():
            rprint(f"  [dim]{category}:[/dim]  {limit // (1024 * 1024)} MB")
        rprint(f"\n[bold]Excluded patterns:[/bold]")
        rprint(f"  [dim]{', '.join(config.excluded_patterns[:8])}...[/dim]")


# -- haydar reindex -------------------------------------------------------------


@app.command()
def reindex() -> None:
    """Force a full re-index of all configured folders."""
    config = HaydarConfig.load()
    _check_initialized(config)

    rprint("[bold]Starting full re-index...[/bold]\n")
    rprint("[yellow]! This will re-process all files. It may take a while.[/yellow]\n")

    confirm = typer.confirm("Continue?", default=True)
    if not confirm:
        raise typer.Exit(0)

    try:
        from haydar.indexer.engine import IndexingEngine

        engine = IndexingEngine(config)
        stats = engine.index_all(force=True)

        rprint("")
        _print_index_stats(stats)
        rprint("\n[green bold]+ Re-index complete![/green bold]")
    except Exception as exc:
        rprint(f"[red]x Re-indexing failed: {exc}[/red]")
        raise typer.Exit(1)


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
    pass


# -- Helpers --------------------------------------------------------------------


def _check_initialized(config: HaydarConfig) -> None:
    """Abort if Haydar hasn't been initialized."""
    if not config.initialized:
        rprint(
            "[red]x Haydar is not initialized.[/red]\n"
            "[dim]Run [bold]haydar init[/bold] first to set up your index.[/dim]"
        )
        raise typer.Exit(1)


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
