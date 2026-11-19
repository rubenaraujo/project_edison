#!/bin/bash
# Compila, monta o .app e assina (certificado ad-hoc estável para manter as permissões).
set -euo pipefail
cd "$(dirname "$0")"
APP="${EDISON_APP:-$HOME/Applications/Edison.app}"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/Edison" Sources/main.swift 2>&1 | grep -v "^$" || true
[ -x "$APP/Contents/MacOS/Edison" ]
mkdir -p "$APP/Contents/Resources"
cp assets/Edison.icns assets/menubar@2x.png "$APP/Contents/Resources/"
pwd > "$APP/Contents/Resources/project_root.txt"
cp Info.plist "$APP/Contents/Info.plist"
# A stable identity keeps privacy permissions across rebuilds (see make_cert.sh); ad-hoc is the fallback.
IDENTITY="-"
security find-certificate -c "Edison Local Signing" >/dev/null 2>&1 && IDENTITY="Edison Local Signing"
codesign --force --sign "$IDENTITY" --identifier local.rubenaraujo.edison "$APP"
echo "Signed with: $([ "$IDENTITY" = "-" ] && echo "ad-hoc (run ./make_cert.sh for stable permissions)" || echo "$IDENTITY")"
echo "OK: $APP"
