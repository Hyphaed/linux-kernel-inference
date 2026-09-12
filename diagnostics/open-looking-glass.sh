#!/usr/bin/env bash
# Opens GNOME Shell's Looking Glass (same as Alt+F2 -> "lg" -> Enter)
# without a real keypress, so a CLI session can enable
# org.gnome.Shell.Eval for scripted debugging.
#
# wtype (attempt 1) doesn't work: Mutter refuses the Wayland
# virtual-keyboard protocol to unprivileged clients. ydotool instead
# injects real evdev events via /dev/uinput, which the compositor reads
# exactly like a hardware keyboard.
#
# Run as YOURSELF (not sudo) -- it needs your own Wayland/uinput access:
#   bash diagnostics/open-looking-glass.sh
#
# Installs ydotool via sudo apt if missing (one password prompt), starts
# its daemon for this session only (not enabled to persist across
# reboots/logins), sends Alt+F2 -> lg -> Enter via Linux input-event
# keycodes, then polls org.gnome.Shell.Eval until it responds.
set -uo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "run this as yourself, not root -- it needs your own session" >&2
  exit 1
fi

if ! command -v ydotool >/dev/null 2>&1; then
  echo "Installing ydotool..."
  sudo apt-get install -y ydotool
fi

if [ ! -w /dev/uinput ]; then
  echo "/dev/uinput isn't writable by $(whoami) -- can't inject input" >&2
  exit 1
fi

SOCKET="${XDG_RUNTIME_DIR:-/tmp}/.ydotool_socket"
export YDOTOOL_SOCKET="$SOCKET"

if ! pgrep -u "$(id -u)" -x ydotoold >/dev/null 2>&1; then
  echo "Starting ydotoold for this session..."
  nohup ydotoold --socket-path="$SOCKET" >/tmp/ydotoold.log 2>&1 &
  disown
  sleep 1
fi

echo "Sending Alt+F2 -> lg -> Enter..."
# Linux input-event-codes.h: KEY_LEFTALT=56, KEY_F2=60, KEY_ENTER=28
ydotool key 56:1 60:1 60:0 56:0
sleep 0.8
ydotool type 'lg'
sleep 0.3
ydotool key 28:1 28:0

echo "Waiting for Looking Glass to enable Eval..."
for i in $(seq 1 15); do
  result=$(gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell --method org.gnome.Shell.Eval "1+1" 2>&1)
  if [ "$result" = "(true, '2')" ]; then
    echo "✓ Looking Glass is open, Eval is enabled."
    exit 0
  fi
  sleep 1
done

echo "Eval still not enabled after 15s -- check your screen, Looking Glass may not have opened." >&2
echo "(if the Run dialog opened but 'lg' wasn't typed correctly, just type lg + Enter yourself)" >&2
echo "ydotoold log: /tmp/ydotoold.log" >&2
exit 1
