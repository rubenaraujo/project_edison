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
codesign --force --sign - --identifier local.rubenaraujo.edison "$APP"
echo "OK: $APP"
