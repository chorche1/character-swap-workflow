"""Ladda ner en fristående Node till <appmappen>/.local/node.

Körs av "1 Installera.command" när datorn saknar Node. Node behövs bara för
Remotion — de animerade textremsmallarna. Ingen systeminstallation, inget
lösenord: det blir en helt vanlig mapp inuti appen, och startskriptet lägger
dess bin/ först i PATH.

Versionen slås upp i nodejs.org/dist/index.json (senaste LTS) i stället för
att hårdkodas, så paketet inte åldras. Allt som går fel → exit 1, och
installationen fortsätter utan de animerade mallarna.
"""
from __future__ import annotations

import json
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

INDEX_URL = "https://nodejs.org/dist/index.json"
TIMEOUT = 60


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x64"
    raise RuntimeError(f"okänd processorarkitektur: {m}")


def _latest_lts(arch: str) -> str:
    with urllib.request.urlopen(INDEX_URL, timeout=TIMEOUT) as r:
        releases = json.load(r)
    # Nyckeln i index.json är "osx-arm64-tar" / "osx-x64-tar" och täcker
    # både .tar.gz och .tar.xz. Någon "-tar-gz"-nyckel finns INTE — att
    # gissa på den gav "hittade ingen LTS-version" på varje maskin.
    want = f"osx-{arch}-tar"
    for rel in releases:            # nyast först
        # "lts" är false för icke-LTS, annars kodnamnet ("Jod", "Iron", …)
        if rel.get("lts") and want in rel.get("files", []):
            return rel["version"]   # t.ex. "v22.20.0"
    raise RuntimeError(f"hittade ingen LTS-version med {want}")


def main() -> int:
    dest = Path(__file__).resolve().parents[1] / ".local" / "node"
    if (dest / "bin" / "node").exists():
        print(f"Node finns redan i {dest}")
        return 0
    try:
        arch = _arch()
        version = _latest_lts(arch)
        name = f"node-{version}-darwin-{arch}"
        url = f"https://nodejs.org/dist/{version}/{name}.tar.gz"
        print(f"Hämtar {name} …")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / "node.tar.gz"
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r, \
                    archive.open("wb") as fh:
                shutil.copyfileobj(r, fh)
            with tarfile.open(archive) as tf:
                # Officiella Node-arkiv innehåller bara en toppmapp; vi
                # packar upp i en temp-katalog och flyttar den på plats så
                # ett avbrutet bygge aldrig lämnar en halv installation.
                tf.extractall(tmp_path)
            staged = tmp_path / name
            if not (staged / "bin" / "node").exists():
                raise RuntimeError("arkivet såg inte ut som väntat")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(staged), str(dest))
        print(f"Node {version} installerad i {dest}")
        return 0
    except Exception as e:                       # noqa: BLE001 — allt är mjukt
        print(f"Node kunde inte installeras: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
