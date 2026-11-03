#!/bin/sh
# Copy the firmware's MPC core (the C++ the controller runs) into native/, where
# the Dockerfile compiles it for the simulations (observer/sim/fwcore.py).
#
#   scripts/native.sh            # from the working tree
#   scripts/native.sh --head     # exactly the committed state (releases)
set -e
cd "$(dirname "$0")/.."
root=$(git rev-parse --show-toplevel)
rm -rf native
mkdir -p native
if [ "$1" = "--head" ]; then
    git -C "$root" archive HEAD:firmware -- src/mpc_core.cpp include/hpn/mpc_core.h | tar -x -C native
else
    mkdir -p native/src native/include/hpn
    cp "$root/firmware/src/mpc_core.cpp" native/src/
    cp "$root/firmware/include/hpn/mpc_core.h" native/include/hpn/
fi
echo "native/: MPC core $(cat native/src/mpc_core.cpp native/include/hpn/mpc_core.h | sha256sum | cut -c1-12)"
