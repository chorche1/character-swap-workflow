#!/bin/bash
# Character Swap Editor — starta.
#
# Dubbelklickas varje gång du vill använda Editor. Terminalfönstret som
# öppnas ÄR servern: låt det ligga kvar så länge du jobbar, stäng det när
# du är klar.
set -uo pipefail

cd "$(dirname "$0")" || { echo "Kunde inte hitta appmappen."; exit 1; }
APP="$PWD"

# Node ligger i appmappen (eller på datorn). Måste med i PATH — Remotion
# startas som `npx` när textremsorna renderas.
export PATH="$APP/.local/node/bin:$HOME/.local/bin:$PATH"

UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -z "$UV" ]; then
  printf '\n\033[31mInstallationen verkar inte ha körts.\033[0m\n'
  printf 'Dubbelklicka på "1 Installera" först.\n\n'
  printf 'Tryck Enter för att stänga.\n'; read -r _; exit 1
fi

# Första lediga porten från 8000 — så en redan startad kopia inte krockar.
PORT=8000
while nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; do
  PORT=$((PORT + 1))
  [ "$PORT" -gt 8050 ] && { echo "Hittade ingen ledig port."; exit 1; }
done

URL="http://127.0.0.1:$PORT"
printf '\n\033[1mStartar Editor på %s\033[0m\n' "$URL"
printf 'Webbläsaren öppnas automatiskt när servern svarar (några sekunder).\n'
printf '\033[2mStäng det här fönstret när du är klar — då stängs Editor.\033[0m\n\n'

# Öppna webbläsaren FÖRST när servern faktiskt svarar. (Appens egen
# --open öppnar den innan uvicorn hunnit lyssna, vilket ger ett
# anslutningsfel som ser ut som att något är sönder.)
(
  for _ in $(seq 1 180); do
    if curl -fsS "$URL/api/health" >/dev/null 2>&1; then
      open "$URL"
      exit 0
    fi
    sleep 1
  done
) &

exec "$UV" run character-swap serve --no-open --port "$PORT"
