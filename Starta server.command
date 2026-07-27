#!/bin/bash
# Double-click in Finder to start the Signalhändelser web server.
set -e
cd "$(dirname "$0")"

echo "Signalhändelser — startar servern"
echo

read -r -p "Tillåt gäster på samma WiFi/LAN att logga in (kräver konton skapade under Inställningar)? [j/y = ja, annars nej] " ANSWER
case "$ANSWER" in
  [jJyY]*)
    HOST_ARGS=(--host 0.0.0.0)
    echo "→ Startar med gäståtkomst aktiverad (nåbar från andra enheter på samma nätverk)."
    ;;
  *)
    HOST_ARGS=()
    echo "→ Startar utan gäståtkomst (nåbar endast från den här datorn)."
    ;;
esac

WATCH_ARGS=()
if [ -n "$SIGNAL_EVENTS_PHONE_NUMBER" ]; then
  WATCH_ARGS=(--watch)
  echo "→ Bevakar Signal-grupper i bakgrunden (SIGNAL_EVENTS_PHONE_NUMBER är satt)."
else
  echo "→ SIGNAL_EVENTS_PHONE_NUMBER är inte satt i miljön, så bakgrundsbevakning av"
  echo "  Signal-grupper (--watch) startas inte den här gången. Webbgränssnittet"
  echo "  fungerar ändå som vanligt. Sätt variabeln (t.ex. i ~/.zshrc) för att"
  echo "  aktivera --watch automatiskt nästa gång."
fi

echo
.venv/bin/python -m signal_events serve --port 5001 "${HOST_ARGS[@]}" "${WATCH_ARGS[@]}"

echo
read -r -p "Servern har stoppats. Tryck Enter för att stänga fönstret." _
