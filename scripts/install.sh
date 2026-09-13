#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${BMO_INSTALL_DIR:-/opt/bmo_pi}"
SERVICE_USER="${BMO_SERVICE_USER:-bmo}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer targets Raspberry Pi OS/Linux." >&2
  exit 1
fi
if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required." >&2
  exit 1
fi

apt_available() { apt-cache show "$1" >/dev/null 2>&1; }
PACKAGES=(
  python3-full python3-venv python3-pip python3-picamera2 python3-opencv
  i2c-tools alsa-utils pipewire pipewire-pulse wireplumber libspa-0.2-modules
  libportaudio2 portaudio19-dev tesseract-ocr curl ca-certificates v4l-utils
  rpicam-apps
)
AVAILABLE=()
MISSING=()

echo "[1/8] Refreshing apt metadata"
sudo apt-get update
for pkg in "${PACKAGES[@]}"; do
  if apt_available "$pkg"; then AVAILABLE+=("$pkg"); else MISSING+=("$pkg"); fi
done
if ((${#MISSING[@]})); then
  echo "Skipping unavailable apt packages on this OS: ${MISSING[*]}"
fi
echo "[2/8] Installing available Raspberry Pi OS packages"
sudo apt-get install -y "${AVAILABLE[@]}"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "[3/8] Creating service account $SERVICE_USER"
  sudo useradd --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
else
  echo "[3/8] Service account already exists"
fi
for group in audio video render gpio i2c; do
  if getent group "$group" >/dev/null 2>&1; then
    sudo usermod -a -G "$group" "$SERVICE_USER"
  fi
done

if [[ "$SOURCE_ROOT" != "$TARGET" ]]; then
  echo "[4/8] Installing project to $TARGET"
  sudo mkdir -p "$TARGET"
  tar --exclude=.venv --exclude=.pytest_cache --exclude=.ruff_cache --exclude='__pycache__' \
      --exclude='logs/*.log*' --exclude='data/*.sqlite3*' -C "$SOURCE_ROOT" -cf - . \
    | sudo tar -C "$TARGET" -xf -
else
  echo "[4/8] Project already at $TARGET"
fi
sudo mkdir -p "$TARGET/data" "$TARGET/logs" "$TARGET/models"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$TARGET"

if [[ ! -x "$TARGET/.venv/bin/python" ]]; then
  echo "[5/8] Creating Raspberry Pi OS venv with apt Python packages visible"
  sudo -u "$SERVICE_USER" python3 -m venv --system-site-packages "$TARGET/.venv"
else
  echo "[5/8] Reusing existing venv"
fi
sudo -u "$SERVICE_USER" "$TARGET/.venv/bin/python" -m pip install --upgrade "pip>=25,<27" "setuptools>=75,<82" wheel
sudo -u "$SERVICE_USER" "$TARGET/.venv/bin/python" -m pip install -r "$TARGET/requirements.txt"
sudo -u "$SERVICE_USER" "$TARGET/.venv/bin/python" -m pip install -e "$TARGET" --no-deps
sudo -u "$SERVICE_USER" "$TARGET/.venv/bin/python" -m pip check

if [[ ! -f "$TARGET/.env" ]]; then
  echo "[6/8] Creating .env from .env.example"
  sudo -u "$SERVICE_USER" cp "$TARGET/.env.example" "$TARGET/.env"
else
  echo "[6/8] Preserving existing .env"
fi
sudo chmod 600 "$TARGET/.env"

if [[ ! -s "$TARGET/models/hand_landmarker.task" ]]; then
  echo "[7/8] Fetching official MediaPipe Hand Landmarker model"
  sudo -u "$SERVICE_USER" "$TARGET/scripts/fetch_models.sh"
else
  echo "[7/8] MediaPipe model already present"
fi

echo "[8/8] Installing systemd unit"
sudo cp "$TARGET/systemd/bmo.service" /etc/systemd/system/bmo.service
# Raspberry Pi OS images can differ slightly in which device groups exist. Avoid a
# unit startup failure caused by naming a supplementary group that this image lacks.
EXTRA_GROUPS=()
for group in audio video render gpio i2c; do
  if getent group "$group" >/dev/null 2>&1; then
    EXTRA_GROUPS+=("$group")
  fi
done
if ((${#EXTRA_GROUPS[@]})); then
  sudo sed -i "s/^SupplementaryGroups=.*/SupplementaryGroups=${EXTRA_GROUPS[*]}/" /etc/systemd/system/bmo.service
else
  sudo sed -i '/^SupplementaryGroups=/d' /etc/systemd/system/bmo.service
fi
sudo systemctl daemon-reload
sudo loginctl enable-linger "$SERVICE_USER" >/dev/null 2>&1 || true

echo
echo "Install complete. Next:"
echo "  sudo $TARGET/scripts/configure_pi.sh"
echo "  sudoedit $TARGET/.env"
echo "  sudo -u $SERVICE_USER $TARGET/.venv/bin/python $TARGET/scripts/diagnose_hardware.py"
echo "  sudo systemctl enable --now bmo.service"
