#!/bin/bash
# Edison installer: Python env, models, app build, launch-at-login. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
APP="$HOME/Applications/Edison.app"
say() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

say "Checking system"
[ "$(uname -s)" = "Darwin" ] || die "Edison only runs on macOS."
[ "$(uname -m)" = "arm64" ] || die "Edison needs an Apple Silicon Mac (MLX models)."
MAJOR=$(sw_vers -productVersion | cut -d. -f1)
[ "$MAJOR" -ge 15 ] || die "macOS 15 or newer is required."
command -v swiftc >/dev/null || { xcode-select --install 2>/dev/null || true; die "Install the Xcode Command Line Tools (a dialog was opened), then run ./install.sh again."; }
PY=$(command -v python3.13 || command -v python3.12 || command -v python3.14 || command -v python3.11 || command -v python3 || true)
[ -n "$PY" ] || die "Python 3.11+ not found. Install it with: brew install python"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python 3.11+ is required (found: $("$PY" -V))."
echo "Using $("$PY" -V) at $PY"

say "Python environment"
if [ ! -x venv/bin/python ] || ! venv/bin/python -c 'pass' 2>/dev/null; then
  rm -rf venv
  "$PY" -m venv venv
fi
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

say "Speaker models"
mkdir -p models data
if [ ! -f models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx ]; then
  curl -fL --progress-bar -o /tmp/edison-seg.tar.bz2 \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
  tar -xjf /tmp/edison-seg.tar.bz2 -C models && rm -f /tmp/edison-seg.tar.bz2
fi
if [ ! -f models/wespeaker_en_voxceleb_resnet293_LM.onnx ]; then
  curl -fL --progress-bar -o models/wespeaker_en_voxceleb_resnet293_LM.onnx \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet293_LM.onnx
fi

say "Transcription and language models (about 10 GB the first time)"
venv/bin/python - <<'EOF'
from huggingface_hub import snapshot_download
for repo in ("mlx-community/whisper-large-v3-mlx", "mlx-community/Qwen2.5-14B-Instruct-4bit"):
    print(repo, flush=True)
    snapshot_download(repo)
EOF

say "Signing identity (keeps macOS permissions across updates)"
chmod +x make_cert.sh
./make_cert.sh || echo "Could not create a signing identity; the app will be signed ad-hoc."

say "Building the app"
pkill_old() { pgrep -f "$APP/Contents/MacOS/Edison" 2>/dev/null || true; }
OLD_AGENT="$HOME/Library/LaunchAgents/local.rubenaraujo.edison.plist"
if [ -f "$OLD_AGENT" ]; then   # legacy KeepAlive agent: replaced by the in-app "Launch at login" switch
  launchctl bootout "gui/$(id -u)" "$OLD_AGENT" 2>/dev/null || true
  rm -f "$OLD_AGENT"
fi
for pid in $(pkill_old); do kill "$pid" 2>/dev/null || true; done
chmod +x build.sh transcribe.sh
./build.sh

say "Starting Edison"
open "$APP"

cat <<EOF

✓ Edison is installed.

  • Look for its icon in the menu bar. Allow Microphone and Screen & System Audio Recording when macOS asks.
  • It starts silently at login. Turn that off from the menu bar icon → "Launch at login".
  • Recordings go to ~/Teams Recordings. Open the manager with the icon → "Open Edison…".

EOF
