#!/usr/bin/env bash
#
# Debian server installer for weather-reader.
#
# Run AFTER cloning the repo to /opt/weather-reader:
#     sudo git clone <your-private-repo-url> /opt/weather-reader
#     sudo /opt/weather-reader/deploy/install.sh
#
# Idempotent: safe to re-run to pick up code/config updates.
# Layout:
#     /opt/weather-reader            code (root-owned, read-only)
#     /etc/weather-reader/config.json  settings (edit these)
#     /var/lib/weather-reader/log    readings-YYYYMMDD.jsonl data
#     systemd: weather-reader, weather-http (run as 'weather')
#
set -euo pipefail

REPO=${REPO:-/opt/weather-reader}
LOG_DIR=${LOG_DIR:-/var/lib/weather-reader/log}
ETC=/etc/weather-reader
CFG=/etc/weather-reader/config.json
SVC_USER=weather
APT=${APT:-1}   # set APT=0 to skip the apt phase

if [[ $EUID -ne 0 ]]; then
  echo "run me as root (sudo ./deploy/install.sh)" >&2
  exit 1
fi

echo "==> weather-reader installer (repo: $REPO)"
[[ -d $REPO/reader ]] || { echo "repo not found at $REPO" >&2; exit 1; }

if [[ $APT == 1 ]]; then
  echo "==> installing apt dependencies"
  apt-get update
  apt-get install -y --no-install-recommends \
    git ca-certificates \
    python3-numpy \
    nodejs npm \
    rtl-sdr
fi

echo "==> blacklisting the DVB-T kernel driver (so rtl_sdr owns the dongle)"
install -m 0644 /dev/stdin /etc/modprobe.d/blacklist-weather-rtl28xxu.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true

echo "==> creating service user + log dir"
if ! id -u $SVC_USER >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/weather-reader \
          --shell /usr/sbin/nologin $SVC_USER
fi
install -d -o $SVC_USER -g $SVC_USER -m 0755 $LOG_DIR

echo "==> seeding config ($CFG) if absent"
install -d -m 0755 $ETC
if [[ ! -f $CFG ]]; then
  install -m 0640 -o root -g $SVC_USER $REPO/config.json $CFG
  echo "    wrote $CFG (edit sensor_id / max_wind_m_s / dir_offset if needed)"
else
  echo "    $CFG already present, leaving untouched"
fi

echo "==> npm install (service deps from lockfile)"
if [[ ! -d $REPO/service/node_modules ]]; then
  ( cd $REPO/service && npm ci --omit=dev )
else
  echo "    node_modules present, skipping"
fi
chmod -R a+rX $REPO

echo "==> installing udev rule for the RTL2832U dongle"
install -m 0644 /dev/stdin /etc/udev/rules.d/99-weather-rtlsdr.rules <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0664", GROUP="weather", TAG+="uaccess"
EOF
udevadm control --reload-rules 2>/dev/null || udevadm control --reload || true
udevadm trigger --subsystem-match=usb 2>/dev/null || true

echo "==> installing systemd units"
install -m 0644 $REPO/deploy/weather-reader.service \
                 $REPO/deploy/weather-http.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now weather-http weather-reader
systemctl restart weather-http weather-reader 2>/dev/null || true

echo "==> installing logrotate"
install -m 0644 /dev/stdin /etc/logrotate.d/weather-reader <<EOF
$LOG_DIR/readings-*.jsonl {
    daily
    rotate 30
    missingok
    compress
    copytruncate
    create 0644 $SVC_USER $SVC_USER
}
EOF

echo
echo "==> done. Verify:"
echo "    rtl_test -t                      # dongle visible + not held by DVB driver"
echo "    systemctl status weather-reader weather-http"
echo "    journalctl -u weather-reader -e"
echo "    curl -s http://<host>:8080/current"
echo "    curl -s http://<host>:8080/  (dashboard)"
echo "    # if your station id differs: edit $CFG then: systemctl restart weather-reader"