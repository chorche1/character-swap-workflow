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
    backend = "sqlite" if settings.use_sqlite_state else "json"
    typer.echo(f"Backend:    {backend}")
    s = store().state
    typer.echo(f"Scenes:     {len(s.scenes)}")
    typer.echo(f"Characters: {len(s.characters)}")
    typer.echo(f"Projects:   {len(s.projects)}")
    job_counts: dict[str, int] = {}
    unfiled = 0
    for job in s.jobs.values():
        if job.project_id:
            job_counts[job.project_id] = job_counts.get(job.project_id, 0) + 1
        else:
            unfiled += 1
    for p in s.projects.values():
        typer.echo(f"  - {p.name}: {job_counts.get(p.project_id, 0)} job(s)")
    if unfiled:
        typer.echo(f"  - (Unfiled): {unfiled} job(s)")
    typer.echo(f"Jobs:       {len(s.jobs)}")
    for job in s.jobs.values():
        movement = "yes" if job.movement_prompt else "no"
        proj = s.projects.get(job.project_id).name if job.project_id and s.projects.get(job.project_id) else "Unfiled"
        typer.echo(f"  - {job.job_id} [{proj}]: {len(job.characters)} chars  movement={movement}")
        for jc in job.characters.values():
            typer.echo(f"      {jc.name}: {jc.status}")


@app.command()
def reset(confirm: bool = typer.Option(False, "--yes", help="Confirm.")) -> None:
    """Wipe state (does NOT delete files in output/)."""
    if not confirm:
        typer.echo("Refusing to reset without --yes")
        raise typer.Exit(1)
    from character_swap.state import store
    store().reset()
    typer.echo("State cleared.")


@app.command()
def migrate() -> None:
    """Migrate state.json → state.sqlite3. Idempotent. Use with USE_SQLITE_STATE=1."""
    from character_swap.migrate_state import migrate as _migrate
    result = _migrate()
    if not result.get("migrated"):
        typer.echo(f"No migration performed: {result.get('reason')}")
        return
    typer.echo("Migration complete:")
    for k in ("scenes", "characters", "projects", "jobs"):
        typer.echo(f"  {k}: {result[k]}")
    typer.echo("state.json renamed to state.json.migrated (kept as backup).")
    typer.echo("Set USE_SQLITE_STATE=1 in your .env and restart to use the SQLite backend.")


if __name__ == "__main__":
    app()
