#!/bin/sh
# Generate the ECDSA P-256 key pairs for update signing and observer commands.
#   tools/gen_keys.sh [dir]        (default: firmware/keys, ignored by git)
# Install the *_public.pem files on the portal (Settings -> Security).
# Keep *_private.pem off the controller; give observer_private.pem only to the observer.
set -eu
dir="${1:-$(dirname "$0")/../keys}"
mkdir -p "$dir"
umask 077
for name in ota observer; do
  if [ -e "$dir/${name}_private.pem" ]; then
    echo "$dir/${name}_private.pem exists, kept"
  else
    openssl ecparam -name prime256v1 -genkey -noout -out "$dir/${name}_private.pem"
    echo "created $dir/${name}_private.pem"
  fi
  openssl ec -in "$dir/${name}_private.pem" -pubout -out "$dir/${name}_public.pem" 2>/dev/null
  printf '%s public key fingerprint: ' "$name"
  openssl ec -pubin -in "$dir/${name}_public.pem" -outform DER 2>/dev/null | sha256sum | cut -c1-16
done
