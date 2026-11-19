#!/bin/bash
# Creates a local self-signed code-signing identity ("Edison Local Signing") in the login keychain.
# Signing every build with the same identity keeps macOS privacy permissions (Microphone, Screen Recording)
# across rebuilds; ad-hoc signatures change on every build and make macOS ask again.
set -euo pipefail
NAME="Edison Local Signing"
KC="$HOME/Library/Keychains/login.keychain-db"
if security find-certificate -c "$NAME" "$KC" >/dev/null 2>&1; then echo "Signing identity already exists."; exit 0; fi

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
PW="edison-$$"
/usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$NAME" \
  -keyout "$T/k.pem" -out "$T/c.pem" \
  -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=critical,codeSigning" >/dev/null 2>&1
/usr/bin/openssl pkcs12 -export -inkey "$T/k.pem" -in "$T/c.pem" -out "$T/c.p12" -passout "pass:$PW" >/dev/null 2>&1
security import "$T/c.p12" -k "$KC" -P "$PW" -T /usr/bin/codesign >/dev/null
echo "Created signing identity '$NAME'."
