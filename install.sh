#!/bin/sh
# Argus one-line install:
#
#   curl -fsSL https://raw.githubusercontent.com/ygone-lee/argus/main/install.sh | sh
#
# Downloads Argus into ~/.argus (set ARGUS_DIR to use another folder) and runs install.py from there.
# Running it again updates that copy and re-runs the installer. Installer options go after `sh -s --`:
#
#   curl -fsSL https://raw.githubusercontent.com/ygone-lee/argus/main/install.sh | sh -s -- --no-open
set -eu

REPO="${ARGUS_REPO:-https://github.com/ygone-lee/argus.git}"
DIR="${ARGUS_DIR:-$HOME/.argus}"
PY=/usr/bin/python3

say() { printf '%s\n' "$*"; }
fail() { printf '\nArgus: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || fail "Argus runs on macOS only for now."

# git and /usr/bin/python3 are only placeholders until Apple's Command Line Tools are installed
if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install >/dev/null 2>&1 || true
  fail "Argus needs Apple's Command Line Tools (git and Python 3). Click Install in the window that just opened,
wait for it to finish, then run the same command again."
fi

if [ -d "$DIR/.git" ]; then
  say "Updating Argus in $DIR"
  git -C "$DIR" pull --ff-only --quiet ||
    fail "Couldn't update $DIR (local changes?). Update it with git, or remove the folder and run this again."
elif [ -e "$DIR" ]; then
  fail "$DIR already exists and isn't an Argus download. Move it away, or set ARGUS_DIR to another folder."
else
  say "Downloading Argus into $DIR"
  git clone --quiet --depth 1 "$REPO" "$DIR" || fail "Couldn't download Argus from $REPO."
fi

# Piped from curl, this script is the installer's stdin — hand it the terminal so it can ask to open the board
if [ -t 1 ] && (: </dev/tty) 2>/dev/null; then
  exec "$PY" "$DIR/install.py" "$@" </dev/tty
fi
exec "$PY" "$DIR/install.py" "$@"
