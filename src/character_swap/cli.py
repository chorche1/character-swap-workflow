from __future__ import annotations

import contextlib
import shutil
import subprocess
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
    for k in ("scenes", "characters", "projects", "jobs", "generations"):
        typer.echo(f"  {k}: {result[k]}")
    typer.echo("state.json renamed to state.json.migrated (kept as backup).")
    typer.echo("Set USE_SQLITE_STATE=1 in your .env and restart to use the SQLite backend.")


@app.command("remotion-install")
def remotion_install(
    force: bool = typer.Option(False, "--force",
                                help="Reinstall even if node_modules already exists."),
) -> None:
    """Install Remotion deps + build the in-browser preview bundle.

    Run this once after cloning, and whenever `remotion/package.json` or any
    file under `remotion/src/preview/` changes.

    Requires Node.js ≥ 18 (`node --version`).
    """
    if shutil.which("node") is None:
        typer.echo("error: `node` not found in PATH. Install Node.js >= 18 from https://nodejs.org/", err=True)
        raise typer.Exit(1)
    remotion_dir = settings.project_root / "remotion"
    if not (remotion_dir / "package.json").is_file():
        typer.echo(f"error: no package.json at {remotion_dir}", err=True)
        raise typer.Exit(1)

    node_modules = remotion_dir / "node_modules"
    if force or not node_modules.is_dir():
        typer.echo(f"==> installing Remotion deps in {remotion_dir} ...")
        proc = subprocess.run(["npm", "install"], cwd=str(remotion_dir))
        if proc.returncode != 0:
            typer.echo(f"npm install failed (exit {proc.returncode})", err=True)
            raise typer.Exit(proc.returncode)
    else:
        typer.echo(f"==> node_modules already present at {node_modules}; skipping install (use --force to reinstall)")

    typer.echo("==> building preview bundle (web/static/remotion-preview.js) ...")
    proc = subprocess.run(["npm", "run", "build-preview"], cwd=str(remotion_dir))
    if proc.returncode != 0:
        typer.echo(f"build-preview failed (exit {proc.returncode})", err=True)
        raise typer.Exit(proc.returncode)
    typer.echo("Remotion install + preview build complete.")


if __name__ == "__main__":
    app()
