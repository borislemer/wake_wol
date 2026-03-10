#!/bin/bash
# Install wake_wol service to /opt/wake: copy files, create systemd unit,
# create devices.txt (optionally interactively add machines), set permissions.
# Run as root (e.g. sudo ./install.sh).

set -e

INSTALL_DIR="${INSTALL_DIR:-/opt/wake}"
SERVICE_NAME="wake-wol.service"
SYSTEMD_UNIT="/etc/systemd/system/${SERVICE_NAME}"

# Resolve script directory (where we're running from, e.g. repo or tarball)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo "  Installs wake_wol to ${INSTALL_DIR} by default."
    echo "  Options:"
    echo "    -d DIR   Install directory (default: /opt/wake)"
    echo "    -n       No interactive add; only create empty devices.txt"
    echo "    -h       This help"
}

NO_INTERACTIVE=""
while getopts "d:nh" opt; do
    case "$opt" in
        d) INSTALL_DIR="$OPTARG" ;;
        n) NO_INTERACTIVE=1 ;;
        h) usage; exit 0 ;;
        *) usage; exit 1 ;;
    esac
done

if [[ "$(id -u)" -ne 0 ]]; then
    echo "This script must be run as root (e.g. sudo $0)." >&2
    exit 1
fi

echo "Checking prerequisites (python3, python3-pcapy, iputils-ping)..."

APT_UPDATED=0
ensure_apt_updated() {
    if [[ "$APT_UPDATED" -eq 0 ]]; then
        apt-get update
        APT_UPDATED=1
    fi
}

if ! command -v python3 >/dev/null 2>&1; then
    echo "  python3 not found, installing via apt..."
    ensure_apt_updated
    apt-get install -y python3
fi

if ! python3 -c "import pcapy" >/dev/null 2>&1; then
    echo "  python3-pcapy (pcap bindings) not found, installing via apt..."
    ensure_apt_updated
    apt-get install -y python3-pcapy
fi

if ! command -v ping >/dev/null 2>&1; then
    echo "  iputils-ping not found, installing via apt..."
    ensure_apt_updated
    apt-get install -y iputils-ping
fi

echo "Installing wake_wol to ${INSTALL_DIR}"

mkdir -p "${INSTALL_DIR}"

# Copy service script and optional reference (skip if already installing in-place)
if [[ "$(realpath "${SCRIPT_DIR}")" != "$(realpath "${INSTALL_DIR}")" ]]; then
    cp -p "${SCRIPT_DIR}/wake_wol.py" "${INSTALL_DIR}/"
    if [[ -f "${SCRIPT_DIR}/systemd.example" ]]; then
        cp -p "${SCRIPT_DIR}/systemd.example" "${INSTALL_DIR}/"
    fi
    if [[ -f "${SCRIPT_DIR}/prereqs.txt" ]]; then
        cp -p "${SCRIPT_DIR}/prereqs.txt" "${INSTALL_DIR}/"
    fi
else
    echo "Installer is already running from ${INSTALL_DIR}, skipping file copy."
fi

# Create devices.txt if missing
DEVICES_FILE="${INSTALL_DIR}/devices.txt"
if [[ ! -f "${DEVICES_FILE}" ]]; then
    touch "${DEVICES_FILE}"
    echo "# interface;target_ip;target_port;mac_address[;cooldown_seconds]" >> "${DEVICES_FILE}"
    echo "# Example: ens33;192.168.0.129;11434;AA:BB:CC:DD:EE:FF;1800" >> "${DEVICES_FILE}"
fi

# Systemd unit: use INSTALL_DIR and set WorkingDirectory so devices.txt is found
cat > "${SYSTEMD_UNIT}" << EOF
[Unit]
Description=Wake target host on observed outbound SYN packets to server in list
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/wake_wol.py
Restart=always
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=inherit

[Install]
WantedBy=multi-user.target
EOF

# Permissions: root owns everything under INSTALL_DIR
chown -R root:root "${INSTALL_DIR}"
chmod 755 "${INSTALL_DIR}"
chmod 644 "${INSTALL_DIR}/wake_wol.py"
chmod 640 "${DEVICES_FILE}"
[[ -f "${INSTALL_DIR}/systemd.example" ]] && chmod 644 "${INSTALL_DIR}/systemd.example"
[[ -f "${INSTALL_DIR}/prereqs.txt" ]]    && chmod 644 "${INSTALL_DIR}/prereqs.txt"

# Copy install.sh into INSTALL_DIR for re-runs (e.g. adding devices later)
if [[ -f "${SCRIPT_DIR}/install.sh" ]] && [[ "$(realpath "${SCRIPT_DIR}")" != "$(realpath "${INSTALL_DIR}")" ]]; then
    cp -p "${SCRIPT_DIR}/install.sh" "${INSTALL_DIR}/"
    chmod 755 "${INSTALL_DIR}/install.sh"
    chown root:root "${INSTALL_DIR}/install.sh"
fi

# Interactive: add machines
add_device_line() {
    local iface ip port mac cooldown
    iface="$1"; ip="$2"; port="$3"; mac="$4"; cooldown="${5:-1800}"
    # Normalize MAC to colons
    mac="$(echo "$mac" | sed -E 's/[-:]//g' | sed 's/\(..\)/\1:/g;s/:$//')"
    echo "${iface};${ip};${port};${mac};${cooldown};" >> "${DEVICES_FILE}"
}

if [[ -z "${NO_INTERACTIVE}" ]]; then
    echo ""
    echo "Add inference server(s) to watch. For each machine, you will be prompted for:"
    echo "  interface, target IP, target port, MAC address, cooldown seconds (default 1800)."
    echo "Leave interface empty to finish."
    while true; do
        echo ""
        read -r -p "Network interface (e.g. ens33) [empty=done]: " iface
        [[ -z "$iface" ]] && break
        read -r -p "Target IP: " ip
        read -r -p "Target port (e.g. 11434 for Ollama): " port
        read -r -p "MAC address (WoL): " mac
        read -r -p "Cooldown seconds [1800]: " cooldown
        cooldown="${cooldown:-1800}"
        if [[ -n "$iface" && -n "$ip" && -n "$port" && -n "$mac" ]]; then
            add_device_line "$iface" "$ip" "$port" "$mac" "$cooldown"
            echo "Added: $iface -> $ip:$port"
        else
            echo "Skipped (missing required field)."
        fi
    done
fi

systemctl daemon-reload
echo ""
echo "Installation complete."
echo "  Install dir:  ${INSTALL_DIR}"
echo "  devices:      ${DEVICES_FILE}"
echo "  systemd unit: ${SYSTEMD_UNIT}"
echo ""
echo "Enable and start the service:"
echo "  systemctl enable --now ${SERVICE_NAME}"
echo ""
echo "To add more machines later, edit ${DEVICES_FILE} or run this script again with interactive add."
