# Eltek Flatpack Pi Controller

Complete local dashboard for the confirmed CANable2 + Eltek Flatpack2 setup.

## Install

1. Clone or download this repository onto the Raspberry Pi.
2. Open a terminal in the project folder.
3. Run:

```bash
sudo bash install.sh
```

The installer prints the phone-accessible address, normally `http://<PI-IP>:5000`.

## Included

- Automatic `/dev/ttyACM*` detection and `can0` startup at 125 kbit/s
- Automatic login using serial `11 51 71 10 20 34`
- Live output voltage, current, watts, AC input and temperatures
- CV / CC / alarm / walk-in status
- SQLite logging, 24-hour graph, CSV export and raw CAN monitor
- Temporary voltage/current setpoints, disabled by default
- Guarded persistent default-voltage write
- systemd auto-start

## Safety configuration

Defaults: 44.5–54.4 V, 1–35 A, OVP 57.6 V. Edit `/opt/flatpack-dashboard/config.json` only after confirming battery, BMS, cable and charger limits. Software is not a substitute for BMS protection, fusing or generator protection.

Temporary setpoints use `0x05FF4004` and are refreshed every two seconds while armed. Persistent default voltage uses `0x05009C00` and requires typing `WRITE DEFAULT`.

## Diagnostics

```bash
sudo systemctl status flatpack-can --no-pager
sudo systemctl status flatpack-dashboard --no-pager
sudo journalctl -u flatpack-dashboard -f
```
