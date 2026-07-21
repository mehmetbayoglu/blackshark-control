# blackshark-control

GTK4 control panel for the **Razer BlackShark V3 / V3 Pro** headset on Linux. It
drives the openrazer `razerkraken` driver's sysfs attributes: headphone EQ
(9 slots, 10 bands), mic EQ with 4 presets, sidetone, game/chat balance, in-call
mix, audio prompts, THX, ANC (V3 Pro), and the audio function button mode.

It also **reflects on-board changes live**: turn the dial, press the EQ button,
flip the mic boom, and so on, and the matching control in the app updates within
about a second (the driver caches the headset's own pushes, the app polls that
cache). Battery is shown when the headset reports it.

Supported PIDs:
- `1532:0579` (V3 wired) and `1532:057a` (V3 2.4 GHz dongle)
- `1532:0576` (V3 Pro wired) and `1532:0577` (V3 Pro 2.4 GHz dongle)

## Requirements

- Linux with an openrazer `razerkraken` build that supports the BlackShark V3
  (PR [openrazer/openrazer#2794](https://github.com/openrazer/openrazer/pull/2794)
  or later)
- Python 3.9+, GTK 4, PyGObject
- User in the `openrazer` group (added automatically when installing `openrazer-meta`)

## Install

### Universal (any distro)

```sh
git clone https://github.com/mehmetbayoglu/blackshark-control.git
cd blackshark-control
./install.sh           # system-wide (uses sudo)
./install.sh --user    # ~/.local install, no sudo
```

The script detects missing GTK4/PyGObject and prints the install command for your distro.

### Arch / CachyOS / Manjaro

```sh
cd packaging/arch
makepkg -si
```

### Debian / Ubuntu

```sh
sudo apt install devscripts debhelper dh-python python3-all python3-setuptools
ln -s packaging/debian debian
debuild -us -uc -b
sudo dpkg -i ../blackshark-control_*.deb
```

### Fedora / RHEL

```sh
sudo dnf install rpm-build python3-devel python3-setuptools python3-pip python3-wheel
rpmbuild -bb --define "_sourcedir $PWD/.." packaging/rpm/blackshark-control.spec
sudo dnf install ~/rpmbuild/RPMS/noarch/blackshark-control-*.rpm
```

### Uninstall

```sh
./install.sh --uninstall    # for the universal installer
# or your distro's package manager
```

## Usage

Launch from your application menu (entry: **BlackShark V3 Control**) or run:

```sh
blackshark-control
```

If you're running from a git checkout rather than an install, run the file
directly so you get the checked-out version:

```sh
python3 blackshark_control/app.py
```

The status bar shows the detected device PID and battery. If it says "Device not
found", the driver isn't loaded or the device isn't bound.

### Tabs

- **Sound** — THX toggle; headphone EQ with 9 slots (Default / Game / Movie /
  Music / Esports, plus Custom 1-4); 10-band sliders with auto-apply; a
  "Write to" slot selector; "Reset to Default Values".
- **Enhancement** — Ultra-Low Latency (wireless), ANC + Ambient with level
  (V3 Pro), Game/Chat balance slider, In-call audio mix.
- **Mic** — Sidetone slider (0-15), Mic EQ presets (Default / Esports /
  Broadcast / MicBoost), 10-band Mic EQ sliders with reset, audio function
  button mode, audio prompts toggle.
- **Power** — Wireless power-save timeout.

Mic volume is **not** controlled here: it's standard USB Audio Class 2, so use
`pavucontrol` or your normal audio mixer.

Per-slot custom EQ values persist in `~/.config/blackshark-control.json`.

## Notes on battery

Battery/charging show the value the headset last **pushed**; the app never polls
the device for it. On the plain V3 the firmware answers a battery query only once
per reconnect and drops the RF link on repeats, so polling it would reset the
dongle. As a result the plain-V3 battery figure updates only when the headset
sends a new value (when the percentage actually changes), not continuously. The
V3 Pro reports battery continuously via its telemetry channel.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
