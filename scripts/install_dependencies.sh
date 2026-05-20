#!/usr/bin/env bash 
set -euo pipefail

ADD_DVSWITCH_REPO=1
INSTALL_DVSWITCH=1

while [ $# -gt 0 ]; do
  case "$1" in
    --add-dvswitch-repo)
      ADD_DVSWITCH_REPO=1
      ;;
    --no-add-dvswitch-repo)
      ADD_DVSWITCH_REPO=0
      ;;
    --no-dvswitch)
      INSTALL_DVSWITCH=0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently supports apt-based Debian/Ubuntu systems." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

have_dvswitch_packages() {
  apt-cache show analog-bridge >/dev/null 2>&1 \
    && apt-cache show mmdvm-bridge >/dev/null 2>&1 \
    && apt-cache show md380-emu >/dev/null 2>&1
}

add_dvswitch_repo() {
  if [ -f /etc/apt/sources.list.d/dvswitch.list ]; then
    echo "DVSwitch repository file already exists; refreshing apt metadata"
    apt-get update || true
    if have_dvswitch_packages; then
      return 0
    fi
    echo "Existing DVSwitch repository did not expose required packages; replacing it"
    rm -f /etc/apt/sources.list.d/dvswitch.list
  fi

  tmp_script="$(mktemp /tmp/dvswitch-bookworm.XXXXXX)"
  echo "Installing DVSwitch repository using http://dvswitch.org/bookworm"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL http://dvswitch.org/bookworm -o "$tmp_script"
  elif command -v wget >/dev/null 2>&1; then
    wget -q http://dvswitch.org/bookworm -O "$tmp_script"
  else
    echo "curl or wget is required to fetch the DVSwitch repository bootstrap script" >&2
    rm -f "$tmp_script"
    return 1
  fi
  bash "$tmp_script"
  rm -f "$tmp_script"
}

apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  gnupg \
  ffmpeg \
  git \
  iproute2 \
  python3 \
  python3-pip \
  python3-setuptools \
  python3-venv \
  python3-wheel \
  rsync \
  espeak-ng

if [ "$INSTALL_DVSWITCH" -eq 1 ]; then
  if ! have_dvswitch_packages; then
    if [ "$ADD_DVSWITCH_REPO" -eq 1 ]; then
      add_dvswitch_repo
      apt-get update
    fi
  fi

  if have_dvswitch_packages; then
    apt-get install -y analog-bridge mmdvm-bridge md380-emu qemu-user-static
  else
    cat >&2 <<'EOF_ERR'
DVSwitch helper packages were not found in apt.
Required helper packages: analog-bridge, mmdvm-bridge, md380-emu, qemu-user-static.

Try:
  sudo ./install.sh --add-dvswitch-repo

Or manually run the DVSwitch repository bootstrap, then re-run this installer:
  wget http://dvswitch.org/bookworm
  chmod +x bookworm
  sudo ./bookworm
EOF_ERR
    exit 1
  fi
fi
