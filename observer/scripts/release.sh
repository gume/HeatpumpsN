#!/bin/sh
# Build the deploy bundle of the observer and (with --tag) tag the release.
#
#   scripts/release.sh            # dist/heatpumpsn-observer-<version>.tar.gz
#   scripts/release.sh --tag      # ...and the annotated tag observer-v<version>
#
# The bundle holds what a release machine needs: the package, Dockerfile,
# deploy/ (compose, env.example, guide), README and CHANGELOG. Checks: a
# clean working tree for --tag, the version has a CHANGELOG entry, tests pass.
set -e
cd "$(dirname "$0")/.."
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
name="heatpumpsn-observer-$version"
grep -q "^## $version" CHANGELOG.md || { echo "CHANGELOG.md has no entry for $version" >&2; exit 1; }
if [ "$1" = "--tag" ] && [ -n "$(git status --porcelain -- .)" ]; then
    echo "uncommitted changes in observer/: commit first" >&2; exit 1
fi
if [ -x .venv/bin/python ]; then .venv/bin/python -m pytest -q; else python3 -m pytest -q; fi
mkdir -p dist
tmp=$(mktemp -d)
mkdir "$tmp/$name"
files="observer pyproject.toml README.md CHANGELOG.md Dockerfile .dockerignore deploy"
if [ "$1" = "--tag" ]; then           # exactly the committed state
    git -C "$(git rev-parse --show-toplevel)" archive HEAD:observer -- $files | tar -x -C "$tmp/$name"
else                                  # a test build of the working tree
    cp -r $files "$tmp/$name/"
    find "$tmp/$name" -name __pycache__ -prune -exec rm -rf {} +
    rm -f "$tmp/$name/deploy/.env"
fi
tar -czf "dist/$name.tar.gz" -C "$tmp" "$name"
rm -rf "$tmp"
echo "dist/$name.tar.gz"
if [ "$1" = "--tag" ]; then
    git tag -a "observer-v$version" -m "observer $version" && echo "tagged observer-v$version (push with: git push origin observer-v$version)"
fi
