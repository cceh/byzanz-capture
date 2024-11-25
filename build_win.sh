#!/bin/bash
set -euo pipefail

# Get latest release info from GitHub API
LIBUSB_RELEASE_INFO=$(wget -qO- https://api.github.com/repos/libusb/libusb/releases/latest)

# Extract Windows release asset URL (only .7z file)
LIBUSB_WIN_URL=$(echo "$LIBUSB_RELEASE_INFO" | \
  jq -r '.assets[] | select(.name | endswith(".7z")) | .browser_download_url')
LIBUSB_FILENAME=$(basename "$LIBUSB_WIN_URL")

# Create vendor directory if it doesn't exist
mkdir -p vendor/libusb

# Download and extract
cd vendor
if [ ! -f "$LIBUSB_FILENAME" ]; then
    wget -N "$LIBUSB_WIN_URL"
else
    echo $LIBUSB_FILENAME already exists, skipping download.
fi
7z x "$LIBUSB_FILENAME" -aos -olibusb

# Clean up downloaded archive
#rm libusb.7z

cd ..

pyinstaller --onedir \
    --add-binary /mingw64/lib/libgphoto2_port/0.12.2/:. \
    --add-binary /mingw64/lib/libgphoto2/2.5.31/:. \
    --add-binary ./vendor/libusb/MinGW64/dll/libusb-1.0.dll:. \
    --add-data ui:ui main.py \
    --runtime-hook ./build_win_hook.py \
    --noconfirm \
    --name byzanz-capture
