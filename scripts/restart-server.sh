#!/usr/bin/env bash
# restart-server.sh — säkert (om)starta DEN ENA Character Swap-dev-servern.
#
# Dödar alla körande instanser först (undviker duplicate-server- /
# SQLite-klobber-faran som åt 45 godkännanden 2026-06-11 och gav
# 7-server-incidenten 2026-06-13), startar EN detachad server från
# huvudkatalogen och väntar in health-endpointen.
#
# Säkert att köra från vilken worktree som helst — startar alltid om den
# kanoniska servern i ~/character-swap-workflow.
#
# Använd:  bash ~/character-swap-workflow/scripts/restart-server.sh
# Eller:   skriv  /restart  i Claude Code.
set -uo pipefail

MAIN="/Users/hugonorrbom/character-swap-workflow"
LOG="/tmp/character-swap-serve.log"
UV="$HOME/.local/bin/uv"
URL="http://127.0.0.1:8000/api/health"

echo "==> Letar efter körande servrar…"
PIDS=$(pgrep -f "character-swap serve" 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  echo "    Hittade PID: $(echo "$PIDS" | tr '\n' ' ')— stänger av snällt (SIGTERM)"
  kill $PIDS 2>/dev/null || true
  # ge dem upp till ~8 s att avsluta, annars tvinga ner (SIGKILL)
  for _ in $(seq 1 16); do
    pgrep -f "character-swap serve" >/dev/null 2>&1 || break
    sleep 0.5
  done
  STILL=$(pgrep -f "character-swap serve" 2>/dev/null || true)
  if [ -n "$STILL" ]; then
    echo "    Hänger kvar — tvingar ner: $(echo "$STILL" | tr '\n' ' ')"
    kill -9 $STILL 2>/dev/null || true
  fi
else
  echo "    Inga körde."
fi

# Frigör port 8000 om något fortfarande håller den
PORT_PID=$(lsof -ti :8000 2>/dev/null || true)
[ -n "$PORT_PID" ] && kill -9 $PORT_PID 2>/dev/null || true

echo "==> Startar EN server från $MAIN…"
nohup "$UV" run --directory "$MAIN" character-swap serve --no-open > "$LOG" 2>&1 &
disown

echo "==> Väntar på health…"
if curl -s --retry 60 --retry-connrefused --retry-delay 1 --max-time 120 "$URL" > /tmp/cs-health.json 2>/dev/null; then
  echo "    ✅ Servern svarar:"
  cat /tmp/cs-health.json
  echo
  echo "==> Klart. Lokalt: http://127.0.0.1:8000  ·  iPhone: https://hugos-macbook-pro.taild324ec.ts.net"
  echo "    Logg: $LOG"
else
  echo "    ❌ Servern svarade inte i tid. Sista raderna ur loggen:"
  tail -20 "$LOG" 2>/dev/null
  echo "    Logg: $LOG"
  exit 1
fi
