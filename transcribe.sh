#!/bin/bash
# Usage: transcribe.sh <call_folder>
# Queue (one call at a time) -> mix into call.m4a -> transcribe + identify speakers + summary -> delete raw tracks
set -uo pipefail
DIR="$1"
REPROCESS=0; [ "${2:-}" = "--reprocess" ] && REPROCESS=1
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/venv/bin/python"
LOGDIR="${TEAMS_REC_FOLDER:-$HOME/Teams Recordings}"
mkdir -p "$LOGDIR"
exec >>"$LOGDIR/processing.log" 2>&1
echo "=== $(date '+%F %T') $DIR"

notify() { osascript -e "display notification \"$1\" with title \"Edison\"" >/dev/null 2>&1 || true; }

# One run per call folder
if [ -f "$DIR/.processing" ] && kill -0 "$(cat "$DIR/.processing")" 2>/dev/null; then
  echo "already being processed"; exit 0
fi
echo $$ >"$DIR/.processing"

# Global queue: wait for our turn (stale locks from dead processes are taken over)
QLOCK="$LOGDIR/.queue.lock"
while ! mkdir "$QLOCK" 2>/dev/null; do
  holder="$(cat "$QLOCK/pid" 2>/dev/null || true)"
  if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then rm -rf "$QLOCK"; continue; fi
  sleep 10
done
echo $$ >"$QLOCK/pid"

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP" "$QLOCK" "$DIR/.processing"; }
trap cleanup EXIT

[ "$REPROCESS" = 1 ] && rm -f "$DIR/.attempts"
# Give up after 3 failed attempts (tracks are kept)
n=$(( $(cat "$DIR/.attempts" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$DIR/.attempts"
if [ "$n" -gt 3 ]; then
  notify "Gave up processing a call after 3 failed attempts. Raw audio kept in $(basename "$(dirname "$DIR")")/$(basename "$DIR")."
  exit 0
fi

fail() {
  echo "FAILED: $1"
  [ -d "$DIR/.speakers.prev" ] && { rm -rf "$DIR/speakers"; mv "$DIR/.speakers.prev" "$DIR/speakers"; }
  notify "Processing a call failed ($1). It will be retried the next time Edison starts."
  exit 1
}

KEEP_TRACKS="$("$PY" -c "import json;print(json.load(open('$HERE/config.json')).get('keep_raw_tracks', False))" 2>/dev/null || echo False)"

TRACKS=()
for t in me others; do
  SRC=""
  for ext in caf m4a; do [ -f "$DIR/$t.$ext" ] && SRC="$DIR/$t.$ext" && break; done
  [ -n "$SRC" ] || continue
  afconvert -f WAVE -d LEI16@16000 -c 1 "$SRC" "$TMP/$t.wav" || fail "audio conversion"
  afconvert -f WAVE -d LEI16@48000 -c 1 "$SRC" "$TMP/$t.48.wav" || fail "audio conversion"
  TRACKS+=("$TMP/$t.48.wav")
done

FROM_MIX=0
if [ ${#TRACKS[@]} -eq 0 ]; then
  # Reprocessing after the raw tracks were deleted: use the mixed call (all voices become 'others')
  [ "$REPROCESS" = 1 ] && [ -f "$DIR/call.m4a" ] || { echo "no audio available"; rm -f "$DIR/.attempts"; exit 0; }
  afconvert -f WAVE -d LEI16@16000 -c 1 "$DIR/call.m4a" "$TMP/others.wav" || fail "audio conversion"
  FROM_MIX=1
elif [ "$REPROCESS" = 0 ] || [ ! -f "$DIR/call.m4a" ]; then
  "$PY" "$HERE/mix.py" "$TMP/mix.wav" "${TRACKS[@]}" || fail "mixing"
  afconvert -f m4af -d aac -b 96000 "$TMP/mix.wav" "$DIR/call.m4a" || fail "mixing"
fi

if [ "$REPROCESS" = 1 ]; then
  [ -f "$DIR/transcript.json" ] && cp "$DIR/transcript.json" "$DIR/transcript.prev.json"
  [ -d "$DIR/speakers" ] && mv "$DIR/speakers" "$DIR/.speakers.prev"
fi

cd "$HERE"
"$PY" process_call.py "$DIR" "$TMP" || fail "transcription"

rm -rf "$DIR/.speakers.prev"
# Only delete raw tracks if mix and transcript exist (unless config keeps them for later reprocessing)
if [ "$FROM_MIX" = 0 ] && [ "$KEEP_TRACKS" != "True" ] && [ -s "$DIR/call.m4a" ] && [ -s "$DIR/transcript.md" ]; then
  rm -f "$DIR"/me.caf "$DIR"/others.caf "$DIR"/me.m4a "$DIR"/others.m4a
fi
rm -f "$DIR/.attempts"
echo "done"
