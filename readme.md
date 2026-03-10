# wake_wol

Wake On LAN automation for **on-premise AI inference servers** (e.g. Ollama, LM Studio). The service runs on the machine that sends requests to the inference server (typically a client or a web frontend host). It sniffs outgoing TCP connections to the inference server; when it sees traffic to a configured target, it checks whether the server is responding to ping and, if not, sends a Wake-on-LAN magic packet to bring it up.

## Backstory

This project was originally created to allow [Open WebUI](https://github.com/open-webui/open-webui) to wake an on-premise inference server on demand. The idea is that Open WebUI can keep running on a small always-on machine, while the heavier GPU box with the actual models can stay powered off or suspended. When Open WebUI (or anything else) starts making HTTP requests toward the inference server, `wake_wol.py` sees the outbound connection attempts and triggers Wake-on-LAN if the server is not yet responding to ping.

## How it works

- **Passive sniffing**: `wake_wol.py` listens on one or more network interfaces for outbound TCP SYN packets to configured inference server IP:port pairs (using BPF and pcap).
- **Liveness check**: Before sending WOL, it pings the target host. Only if the host does not respond to ping does it send the magic packet (avoids unnecessary WOL when the server is already up).
- **Cooldown**: Each target has a configurable cooldown (default 5 minutes) that starts only after a WoL packet has been sent to an unreachable server, so repeated failed connection attempts do not flood WOL packets.

## Use case

Use this when your spelling AI inference runs on a separate on-premise machine that is powered off or suspended to save energy. When something (e.g. a web frontend or a desktop app) tries to connect to that inference server, this service detects the attempt and wakes the server via WOL if it is not already responding.

## Installation

Use [install.sh](install.sh). It installs the service under `/opt/wake` by default, creates a systemd unit from the example, creates `devices.txt`, and can interactively add machines (interface, IP, port, MAC, cooldown). Run as root:

```bash
sudo ./install.sh
```

**Options:**

- `-d DIR` — Install to `DIR` instead of `/opt/wake`
- `-n` — No interactive add; only create an empty `devices.txt` (edit manually or re-run later)
- `-h` — Show help

**What the script does:**

- Copies `wake_wol.py`, `systemd.example`, and `prereqs.txt` into the install directory
- Creates `devices.txt` with a comment header if missing
- Installs the systemd unit as `wake-wol.service` with `WorkingDirectory` set so `devices.txt` is found
- Sets permissions: install dir owned by `root:root`, dir `755`, `wake_wol.py` `644`, `devices.txt` `640`
- Copies `install.sh` into the install dir so you can run it again from `/opt/wake` to add more machines

**After install:**

```bash
systemctl enable --now wake-wol.service
```

To add more machines later, edit `/opt/wake/devices.txt` or run `sudo /opt/wake/install.sh` again (without `-n`) to add interactively.

## Configuration

- **devices.txt**: One line per inference server. Format:  
  `interface;target_ip;target_port;mac_address[;cooldown_seconds]`  
  Example: `ens33;192.168.0.129;11434;AA:BB:CC:DD:EE:FF;1800`
- **systemd**: The unit runs `wake_wol.py` from `/opt/wake` with `WorkingDirectory=/opt/wake` so the default `devices.txt` path is used.

## Requirements

- Python 3 with `python3-pcapy` and `iputils-ping` (see [prereqs.txt](prereqs.txt))
- Root (or CAP_NET_RAW/CAP_NET_ADMIN) to capture packets
- Wake-on-LAN enabled on the inference server NIC and BIOS
