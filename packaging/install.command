#!/bin/bash
# Character Swap Editor — engångsinstallation.
#
# Körs genom att dubbelklickas i Finder. Installerar allt som behövs i den
# HÄR mappen (och i ~/.local/bin för uv) — inget systempaket, inget
# administratörslösenord, inget Homebrew.
#
# Ordning: karantän av → uv → Python + paket → Node → Remotion.
# Varje steg rapporterar högt om det misslyckas. Node/Remotion är det enda
# steget som får misslyckas utan att stoppa installationen (då saknas de
# animerade textremsmallarna, resten fungerar).
set -uo pipefail

cd "$(dirname "$0")" || { echo "Kunde inte hitta appmappen."; exit 1; }
APP="$PWD"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n\n' "$*"; printf 'Tryck Enter för att stänga.\n'; read -r _; exit 1; }

printf '\033[1m Character Swap Editor — installation\033[0m\n'
printf ' Mapp: %s\n' "$APP"
printf ' Det här tar 5–15 minuter och laddar ner ca 700 MB.\n'
printf ' Låt fönstret vara öppet tills det står KLART.\n'

# --- 1. macOS-karantän ------------------------------------------------------
# Filer ur en nedladdad zip flaggas av Gatekeeper. Tas flaggan bort här
# slipper användaren högerklick → Öppna på startfilen.
say "1/5  Släpper macOS-karantänen"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null
ok "klart"

# --- 2. uv ------------------------------------------------------------------
say "2/5  Kontrollerar uv (paket- och Python-hanteraren)"
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -z "$UV" ]; then
  echo "  uv saknas — installerar (inget lösenord behövs)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
  if [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
fi
[ -n "$UV" ] || die "Kunde inte installera uv. Kolla internetanslutningen och kör filen igen."
ok "uv: $UV"

# --- 3. Python + paket ------------------------------------------------------
# uv hämtar en egen Python 3.11+ (macOS systempython är 3.9 och duger inte).
say "3/5  Installerar Python och alla paket (det här är det längsta steget)"
if [ -f "$APP/uv.lock" ]; then
  "$UV" sync --frozen || {
    warn "låsta versioner gick inte att installera — försöker utan låsning"
    "$UV" sync || die "Paketinstallationen misslyckades. Kolla internet och kör filen igen."
  }
else
  "$UV" sync || die "Paketinstallationen misslyckades. Kolla internet och kör filen igen."
fi
ok "Python och paket på plats"

# --- 4. Node ----------------------------------------------------------------
# Behövs bara för de animerade textremsmallarna (Remotion). Laddas ner som en
# vanlig mapp här inne — ingen systeminstallation.
say "4/5  Kontrollerar Node (krävs för de animerade textremsorna)"
NODE_OK=0
if [ -x "$APP/.local/node/bin/node" ]; then
  export PATH="$APP/.local/node/bin:$PATH"
  NODE_OK=1
  ok "Node finns redan i appmappen"
elif command -v node >/dev/null 2>&1; then
  NODE_OK=1
  ok "Node finns på datorn: $(node --version 2>/dev/null)"
else
  echo "  Node saknas — laddar ner…"
  if "$UV" run python .setup/install_node.py; then
    export PATH="$APP/.local/node/bin:$PATH"
    NODE_OK=1
    ok "Node installerad i appmappen"
  else
    warn "Node kunde inte installeras."
  fi
fi

# --- 5. Remotion ------------------------------------------------------------
say "5/5  Bygger de animerade textremsmallarna"
REMOTION_OK=0
if [ "$NODE_OK" = "1" ]; then
  if "$UV" run character-swap remotion-install; then
    REMOTION_OK=1
    ok "textremsmallarna klara"
  else
    warn "Remotion-bygget misslyckades."
  fi
fi

printf '\n'
if [ "$REMOTION_OK" = "1" ]; then
  printf '\033[1;32m KLART.\033[0m Dubbelklicka nu på "2 Starta Editor".\n\n'
else
  printf '\033[1;32m KLART — men utan de animerade textremsorna.\033[0m\n'
  printf ' Editor fungerar (klippning, tempo, röstbyte, vanliga textremsor),\n'
  printf ' men de animerade mallarna syns inte i listan. Kör den här filen\n'
  printf ' igen när du har internet så görs ett nytt försök.\n'
  printf ' Dubbelklicka nu på "2 Starta Editor".\n\n'
fi
printf 'Tryck Enter för att stänga fönstret.\n'
read -r _
