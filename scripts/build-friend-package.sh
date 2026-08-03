#!/bin/bash
# Bygg ett fristående Editor-paket att skicka till någon annan.
#
#   ./scripts/build-friend-package.sh [målmapp]
#
# Resultat: <målmapp>/character-swap-editor-ÅÅÅÅ-MM-DD.zip (default ~/Desktop)
#
# Vad paketet ÄR: appens spårade filer, reducerade till Editor-fliken, med
# OPENAI_API_KEY + ELEVENLABS_API_KEY förifyllda, plus två dubbelklickbara
# skript och en svensk guide. Mottagaren behöver INTE Homebrew, Python,
# Node eller administratörslösenord — installationsskriptet ordnar allt.
#
# Vad paketet INTE innehåller: dev-dokumentationen, testerna, scripts/,
# .claude/, någon .git, någon genererad media, och inga andra API-nycklar
# än de två ovan (så mottagaren inte kan bränna pengar på videogenerering).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${1:-$HOME/Desktop}"
STAMP="$(date +%Y-%m-%d)"
NAME="character-swap-editor"
SECRETS_ENV="${SECRETS_ENV:-$HOME/character-swap-data/.env}"

cd "$REPO"
command -v git >/dev/null || { echo "git saknas"; exit 1; }
UV="$(command -v uv 2>/dev/null || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
[ -n "$UV" ] || { echo "uv saknas"; exit 1; }

BUILD="$(mktemp -d)/$NAME"
mkdir -p "$BUILD"
trap 'rm -rf "$(dirname "$BUILD")"' EXIT

echo "▸ Kopierar spårade filer …"
# git ls-files = ENDAST spårade filer. Det utesluter automatiskt .env,
# .venv/, node_modules/, output/, state/ och allt annat ignorerat — till
# skillnad från `zip -r`, som hade fått med Hugos nycklar och alla renders.
if [ -n "$(git status --porcelain)" ]; then
  echo "  (arbetskopian har ocommittade ändringar — de följer med i paketet)"
fi
git ls-files -z | rsync -a --files-from=- --from0 ./ "$BUILD/"

echo "▸ Plockar bort utvecklings- och privatfiler …"
rm -rf "$BUILD/tests" "$BUILD/scripts" "$BUILD/.claude" "$BUILD/packaging"
rm -f  "$BUILD/CLAUDE.md" "$BUILD/AGENTS.md" \
       "$BUILD/WORKFLOWS_GUIDE.md" "$BUILD/ux-improvements.md" \
       "$BUILD/.gitignore"
rm -f  "$BUILD"/RELIABILITY_AUDIT_*.md
# README.md kan INTE bara raderas: pyproject.toml pekar ut den som
# `readme`, och hatchling vägrar bygga paketet utan den ("Readme file does
# not exist") — vilket får hela installationen hos mottagaren att fallera.
# Den ersätts med en pekare till den riktiga guiden.
printf '# Character Swap Editor\n\nSe "LÄS MIG.md" för installation och användning.\n' \
  > "$BUILD/README.md"

echo "▸ Reducerar till Editor-fliken …"
"$UV" run python packaging/editor_only.py "$BUILD"

echo "▸ Låser paketversionerna …"
"$UV" lock >/dev/null
cp uv.lock "$BUILD/uv.lock"

echo "▸ Skriver .env …"
# Endast de två nycklar Editor faktiskt använder. Värdena läses ur den
# delade .env och skrivs direkt till fil — de passerar aldrig terminalen.
[ -f "$SECRETS_ENV" ] || { echo "hittar inte $SECRETS_ENV"; exit 1; }
{
  echo "# Character Swap Editor — inställningar."
  echo "# Nycklarna nedan tillhör den som gav dig kopian."
  echo ""
  grep -E '^OPENAI_API_KEY=.+'     "$SECRETS_ENV" | head -1
  grep -E '^ELEVENLABS_API_KEY=.+' "$SECRETS_ENV" | head -1
  echo ""
  echo "# Taket för hur stora filer som får laddas upp (~2 GB)."
  echo "# Standardvärdet 25 MB avvisar en vanlig mobilinspelning."
  echo "MAX_UPLOAD_BYTES=2000000000"
} > "$BUILD/.env"
for want in OPENAI_API_KEY ELEVENLABS_API_KEY; do
  grep -q "^$want=." "$BUILD/.env" || { echo "  ✗ $want saknas i $SECRETS_ENV"; exit 1; }
done
chmod 600 "$BUILD/.env"

echo "▸ Lägger in installations- och startfilerna …"
cp "packaging/install.command" "$BUILD/1 Installera.command"
cp "packaging/start.command"   "$BUILD/2 Starta Editor.command"
cp "packaging/LÄS MIG.md"      "$BUILD/LÄS MIG.md"
chmod +x "$BUILD/1 Installera.command" "$BUILD/2 Starta Editor.command"
mkdir -p "$BUILD/.setup"
cp "packaging/install_node.py" "$BUILD/.setup/install_node.py"

echo "▸ Sista kontroll …"
# Skyddsnät: inga andra nycklar än de två avsedda, ingen .git, inga renders.
LEAKS="$(grep -rIl -E '(sk-[A-Za-z0-9_-]{20}|xai-[A-Za-z0-9]{20}|AIza[A-Za-z0-9_-]{30}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30})' "$BUILD" 2>/dev/null | grep -v '^'"$BUILD"'/\.env$' || true)"
[ -z "$LEAKS" ] || { echo "  ✗ nyckelliknande strängar hittade i:"; echo "$LEAKS"; exit 1; }
[ ! -e "$BUILD/.git" ] || { echo "  ✗ .git följde med"; exit 1; }
PERSONAL="$(grep -rIl -e hugonorrbom -e taild324ec "$BUILD" 2>/dev/null || true)"
[ -z "$PERSONAL" ] || { echo "  ✗ personliga sökvägar kvar:"; echo "$PERSONAL"; exit 1; }
echo "  ✓ inga läckor"

mkdir -p "$DEST_DIR"
ZIP="$DEST_DIR/$NAME-$STAMP.zip"
rm -f "$ZIP"
( cd "$(dirname "$BUILD")" && zip -qry "$ZIP" "$NAME" -x '*.DS_Store' )

printf '\n✓ Klart: %s (%s)\n' "$ZIP" "$(du -h "$ZIP" | cut -f1 | tr -d ' ')"
printf '  Innehåller dina OpenAI- och ElevenLabs-nycklar i klartext —\n'
printf '  skicka den som en privat fil, inte via en öppen länk.\n\n'
