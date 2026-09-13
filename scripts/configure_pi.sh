#!/usr/bin/env bash
set -euo pipefail
ROOT="${BMO_INSTALL_DIR:-/opt/bmo_pi}"
SERVICE_USER="${BMO_SERVICE_USER:-bmo}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_i2c 0
  echo "I2C enabled."
else
  echo "raspi-config not found; enable I2C manually before using the OLED."
fi

BOOTCFG="/boot/firmware/config.txt"
if [[ -f "$BOOTCFG" ]] && ! grep -Eq '^camera_auto_detect=1$' "$BOOTCFG"; then
  printf '\n# BMO Pi camera autodetection\ncamera_auto_detect=1\n' >> "$BOOTCFG"
  echo "Enabled camera_auto_detect=1 in $BOOTCFG"
fi

USER_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
if [[ -n "$USER_HOME" && -f "$ROOT/config/pipewire/echo-cancel.conf.example" ]]; then
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$USER_HOME/.config/pipewire/pipewire.conf.d"
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0644 \
    "$ROOT/config/pipewire/echo-cancel.conf.example" \
    "$USER_HOME/.config/pipewire/pipewire.conf.d/60-bmo-echo-cancel.conf"
  loginctl enable-linger "$SERVICE_USER" >/dev/null 2>&1 || true
  echo "Installed PipeWire WebRTC echo-cancel drop-in for $SERVICE_USER."
  echo "It becomes active when that user's PipeWire instance is running; reboot is the simplest first setup."
fi

echo "Configuration written. Reboot is recommended if I2C/camera settings changed."
