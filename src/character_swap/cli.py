from __future__ import annotations

import contextlib
import webbrowser

import typer
import uvicorn

from character_swap.config import settings

app = typer.Typer(help="Character Swap Studio — web UI.")


@app.command()
def serve(
    host: str = typer.Option(None, help="Bind host (default 127.0.0.1)."),
    port: int = typer.Option(None, help="Bind port (default 8000)."),
    reload: bool = typer.Option(False, help="Enable auto-reload for development."),
    open_browser: bool = typer.Option(True, "--open/--no-open",
                                      help="Open the browser on startup."),
) -> None:
    """Start the FastAPI server and open the studio in your browser."""
    h = host or settings.host
    p = port or settings.port

    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(f"http://{h}:{p}")

    uvicorn.run(
        "character_swap.api:app",
        host=h,
        port=p,
        reload=reload,
        log_level="info",
    )


@app.command()
def status() -> None:
    """Print a quick text summary of the persisted state."""
    from character_swap.state import store
    s = store().state
    typer.echo(f"Scenes:     {len(s.scenes)}")
    typer.echo(f"Characters: {len(s.characters)}")
    typer.echo(f"Jobs:       {len(s.jobs)}")
    for job in s.jobs.values():
        movement = "yes" if job.movement_prompt else "no"
        typer.echo(f"  - {job.job_id}: {len(job.characters)} chars  movement={movement}")
        for jc in job.characters.values():
            typer.echo(f"      {jc.name}: {jc.status}")


@app.command()
def reset(confirm: bool = typer.Option(False, "--yes", help="Confirm.")) -> None:
    """Wipe state.json (does NOT delete files in output/)."""
    if not confirm:
        typer.echo("Refusing to reset without --yes")
        raise typer.Exit(1)
    from character_swap.state import store
    store().reset()
    typer.echo("State cleared.")


if __name__ == "__main__":
    app()
