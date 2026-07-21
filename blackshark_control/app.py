#!/usr/bin/env python3
"""BlackShark V3 control panel — real-time testing GUI for the razerkraken driver fork."""

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib, Gdk, Pango
import glob, os, subprocess, json, threading

SYSFS_DIR = '/sys/bus/hid/drivers/razerkraken'
PIDS = ('0576', '0577', '057A', '0579')   # V3 Pro wired, V3 Pro 2.4GHz, V3 wireless dongle, V3 wired

# Per-PID feature/capability map. Each entry is the sysfs attr name (or None).
# A feature with attr=None is hidden in the GUI for that device.
DEVICE_CAPS = {
    # V3 wireless dongle (full V3 feature set)
    '057A': {
        'name': 'BlackShark V3 (2.4 GHz)',
        'thx': 'thx_spatial_audio',
        'ull': 'ultra_low_latency',
        'sidetone': 'sidetone',
        'eq': 'headphone_eq',         # accepts "<profile> b1..b10"
        'eq_mode': 'bands',           # full per-band control
        'power_save': 'wireless_power_save',
        'battery': 'charge_level',    # 0..255 byte, scale /255*100 for %
        'charging': 'charge_status',
        'anc': None,
        'mic_eq': 'mic_eq',
        'mic_eq_preset': 'mic_eq_preset',
        'audio_fn_button': 'audio_function_button',
        'mic_volume_uac2': True,
        'game_chat': 'game_chat_balance',
        'in_call_mix': 'in_call_audio_mix',
        'audio_prompts': 'audio_prompts',
    },
    # V3 wired
    '0579': None,   # filled below — same as 057A minus ull/power_save/battery
    # V3 Pro
    '0577': {
        'name': 'BlackShark V3 Pro',
        'thx': 'v3pro_thx_spatial_audio',
        'ull': 'v3pro_ultra_low_latency',
        'sidetone': 'v3pro_sidetone',
        'eq': 'v3pro_headphone_eq',   # accepts single byte = slot index 0..8
        'eq_mode': 'slot-only',       # only switch between firmware slots
        'power_save': 'v3pro_power_save',
        'battery': 'charge_level',          # 0..255 byte, scale /255*100 for %
        'charging': 'charge_status',
        'anc': 'v3pro_anc',
        'mic_eq': 'mic_eq',           # shared with V3 — same 0x97 protocol
        'mic_eq_preset': 'mic_eq_preset',
        'audio_fn_button': 'audio_function_button',  # same wire bytes as V3 (cls=0xea, 1-byte arg)
        'mic_volume_uac2': True,
        'game_chat': 'game_chat_balance',
        'in_call_mix': 'in_call_audio_mix',
        'audio_prompts': 'audio_prompts',
    },
}
# V3 wired: same as wireless but no power-save / ULL (those need the dongle's
# wireless link). Battery is irrelevant when wired.
DEVICE_CAPS['0579'] = dict(DEVICE_CAPS['057A'])
DEVICE_CAPS['0579'].update({
    'name': 'BlackShark V3 (Wired)',
    'ull': None,
    'power_save': None,
})
# V3 Pro wired: same as V3 Pro wireless minus power_save/ULL (those need the
# wireless link). Battery is still present — the headset is just charging
# whenever it's plugged in via USB-C.
DEVICE_CAPS['0576'] = dict(DEVICE_CAPS['0577'])
DEVICE_CAPS['0576'].update({
    'name': 'BlackShark V3 Pro (Wired)',
    'power_save': None,
    'ull': None,
})

EQ_FREQS = ['31Hz','63Hz','125Hz','250Hz','500Hz','1kHz','2kHz','4kHz','8kHz','16kHz']

EQ_PRESETS = {
    'Default':  [ 0,  0,  0,  0,  0,  0,  0,  0,  0,  0],
    'Game':     [ 2,  2, -3, -3,  1, -1,  2,  3,  3,  3],
    'Movie':    [ 3,  3,  4,  0, -4, -4,  2,  3,  3,  3],
    'Music':    [ 6,  3,  4,  3,  0,  0,  0,  1,  3,  4],
    'Esports':  [ 1,  1, -1,  0,  2,  0,  4,  4,  4, -3],
    # Firmware slots 5..8 are user-writable. They start flat; whatever you
    # dial in and Apply gets saved back to that slot.
    'Custom 1': [ 0,  0,  0,  0,  0,  0,  0,  0,  0,  0],
    'Custom 2': [ 0,  0,  0,  0,  0,  0,  0,  0,  0,  0],
    'Custom 3': [ 0,  0,  0,  0,  0,  0,  0,  0,  0,  0],
    'Custom 4': [ 0,  0,  0,  0,  0,  0,  0,  0,  0,  0],
}
PRESET_IDX = {'Default': 0, 'Game': 1, 'Movie': 2, 'Music': 3, 'Esports': 4,
              'Custom 1': 5, 'Custom 2': 6, 'Custom 3': 7, 'Custom 4': 8}
# Slot label per firmware EQ index, in order (0..8).
PROFILE_NAMES = [n for n, _ in sorted(PRESET_IDX.items(), key=lambda kv: kv[1])]

MIC_EQ_PRESETS = ['Default', 'Esports', 'Broadcast', 'MicBoost']
# Synthetic factory values (Synapse doesn't send mic EQ band data with preset cmds)
MIC_EQ_PRESET_BANDS = {
    'Default':   [ 0,  0,  0,  0,  0,  0,  0,  0,  0,  0],
    'Esports':   [-3, -3, -1, -1,  0,  0,  3,  4,  2,  1],
    'Broadcast': [-4,  1,  2,  0, -1,  0,  2,  3,  2, -1],
    'MicBoost':  [ 2,  2,  3,  4,  4,  4,  4,  3,  2,  2],
}
MIC_EQ_FACTORY = {i: list(v) for i, (_, v) in enumerate(MIC_EQ_PRESET_BANDS.items())}
# Factory (Razer built-in) values per profile slot
EQ_FACTORY = {PRESET_IDX[n]: list(v) for n, v in EQ_PRESETS.items()}

CONFIG_FILE = os.path.expanduser('~/.config/blackshark-control.json')

def _read_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _write_config(data):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def load_eq_config():
    saved = {int(k): v for k, v in _read_config().get('eq_custom', {}).items()}
    for idx in EQ_FACTORY:
        if idx not in saved:
            saved[idx] = list(EQ_FACTORY[idx])
    return saved

def save_eq_config(eq_custom):
    data = _read_config()
    data['eq_custom'] = {str(k): v for k, v in eq_custom.items()}
    _write_config(data)

def load_mic_eq_config():
    saved = {int(k): v for k, v in _read_config().get('mic_eq_custom', {}).items()}
    for idx in range(4):
        if idx not in saved:
            saved[idx] = list(MIC_EQ_FACTORY[idx])
    return saved

def save_mic_eq_config(mic_eq_custom):
    data = _read_config()
    data['mic_eq_custom'] = {str(k): v for k, v in mic_eq_custom.items()}
    _write_config(data)

def load_state(pid):
    """Per-device state cache: feature → last-written string value.
    Used as fallback when the driver returns -1 (no SET this session)."""
    return _read_config().get('state', {}).get(pid, {})

def save_state_value(pid, feature, value):
    data = _read_config()
    state = data.setdefault('state', {}).setdefault(pid, {})
    state[feature] = str(value)
    _write_config(data)

CSS = b"""
* { font-family: "Noto Sans", sans-serif; }
window, .main-box { background-color: #111111; color: #eeeeee; }
notebook > header > tabs > tab { background: #1a1a1a; color: #888; padding: 8px 18px; border: none; }
notebook > header > tabs > tab:checked { background: #1a1a1a; color: #00ff41; border-bottom: 2px solid #00ff41; }
notebook > header { background: #1a1a1a; border-bottom: 1px solid #333; }
.section-label { color: #00ff41; font-weight: bold; font-size: 11px; letter-spacing: 1px; }
.card { background: #1e1e1e; border-radius: 6px; padding: 16px; }
scale trough { background: #2a2a2a; min-height: 4px; min-width: 4px; }
scale trough highlight { background: #00ff41; }
scale slider { background: #00ff41; border-radius: 6px; }
scale.vertical trough { min-width: 4px; min-height: 4px; }
.freq-label { color: #888; font-size: 9px; }
.db-label { color: #555; font-size: 9px; }
.value-label { color: #00ff41; font-size: 10px; font-weight: bold; }
.preset-btn { background: #2a2a2a; color: #aaa; border: 1px solid #333; border-radius: 4px; padding: 6px 10px; font-size: 11px; }
.preset-btn:hover { background: #333; color: #eee; }
.preset-btn.active { background: #003a12; color: #00ff41; border-color: #00ff41; }
.toggle-on { background: #003a12; color: #00ff41; border-radius: 4px; padding: 4px 12px; border: 1px solid #00ff41; }
.toggle-off { background: #2a2a2a; color: #666; border-radius: 4px; padding: 4px 12px; border: 1px solid #444; }
.apply-btn { background: #00ff41; color: #000; border-radius: 4px; padding: 8px 20px; font-weight: bold; font-size: 12px; border: none; }
.apply-btn:hover { background: #00cc33; }
.status-ok { color: #00ff41; font-size: 10px; }
.status-err { color: #ff4444; font-size: 10px; }
.pwr-btn { background: #2a2a2a; color: #888; border: 1px solid #333; border-radius: 4px; padding: 6px 14px; }
.pwr-btn.active { background: #003a12; color: #00ff41; border-color: #00ff41; }
.hex-view { background: #0d0d0d; color: #00ff41; font-family: monospace; font-size: 10px; padding: 8px; border-radius: 4px; }
"""

def sysfs_path():
    """Return (path, pid) for the first connected BlackShark variant."""
    for pid in PIDS:
        paths = glob.glob(f'{SYSFS_DIR}/0003:1532:{pid}.*')
        if paths:
            return paths[0], pid
    return None, None


class Device:
    """Capability-aware wrapper around the kernel sysfs interface.

    Hides per-device differences (V3 vs V3 Pro use different attribute names
    for the same logical feature). Use `dev.has(feature)` to gate UI, and
    `dev.write(feature, val)` / `dev.read(feature)` to talk to it.
    """
    def __init__(self, path, pid):
        self.path = path
        self.pid = pid
        self.caps = DEVICE_CAPS[pid]
        self.name = self.caps['name']
        self.is_v3_pro = (pid in ('0576', '0577'))
        self.state = load_state(pid)

    @classmethod
    def detect(cls):
        path, pid = sysfs_path()
        return cls(path, pid) if path else None

    def has(self, feature):
        return self.caps.get(feature) is not None

    def attr(self, feature):
        return self.caps.get(feature)

    def write(self, feature, value):
        attr = self.attr(feature)
        if attr is None:
            return False, f'feature {feature!r} unsupported on {self.name}'
        try:
            with open(f'{self.path}/{attr}', 'w') as f:
                f.write(str(value))
            self.state[feature] = str(value)
            save_state_value(self.pid, feature, value)
            return True, None
        except Exception as e:
            return False, str(e)

    def read(self, feature):
        attr = self.attr(feature)
        if attr is None:
            return None
        try:
            with open(f'{self.path}/{attr}') as f:
                return f.read().strip()
        except Exception:
            return None

    def get_value(self, feature, fallback=None):
        """Driver cache > JSON cache > fallback. Returns the raw string or fallback.
        '-1' from the driver means 'no SET this session' — treated as no value."""
        v = self.read(feature)
        if v is not None and v != '-1' and not v.startswith('-1 '):
            return v
        return self.state.get(feature, fallback)

    def get_value_cached(self, feature, fallback=None):
        """JSON cache > fallback only — never touches sysfs. Use during startup
        to avoid blocking on GETs that take 2s when the firmware response gate
        is tripped (V3 wireless on Linux). The 2s _refresh_status timer will
        sync from sysfs once the window is up."""
        return self.state.get(feature, fallback)


def sysfs_read(attr):
    """Legacy direct-attr read (used by code that already knows the attr name)."""
    path, _ = sysfs_path()
    if not path:
        return None
    try:
        with open(f'{path}/{attr}') as f:
            return f.read().strip()
    except Exception:
        return None

def sysfs_write(attr, value):
    """Legacy direct-attr write."""
    path, _ = sysfs_path()
    if not path:
        return False, 'device not found'
    try:
        with open(f'{path}/{attr}', 'w') as f:
            f.write(str(value))
        return True, None
    except Exception as e:
        return False, str(e)


class BlackSharkControl(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='BlackShark Control')
        self.set_default_size(900, 620)
        self.set_resizable(False)

        self._device = Device.detect()

        self._eq_sliders = []
        self._eq_values = [0] * 10
        self._eq_apply_timer = None
        self._eq_custom = load_eq_config()
        self._mic_eq_custom = load_mic_eq_config()
        self._connected_pid = self._device.pid if self._device else None
        self._mic_vol_slider = None
        self._ignore_slider = False
        # When True, a widget is being updated programmatically to mirror an
        # on-board (headset button) change — its own change handler must NOT
        # write the value back to the device (that would be a feedback loop).
        self._syncing = False
        self._sync_inflight = False

        # Seed UI state from JSON cache → defaults. Skip sysfs reads at startup
        # because some attrs (thx, ull, battery on V3 wireless) issue blocking
        # GETs that take ~2s each when the firmware response gate is tripped.
        # _refresh_status (2s timer) re-syncs from sysfs in the background.
        def _gv(feature, default):
            v = self._device.get_value_cached(feature, default) if self._device else default
            return v if v is not None else default
        def _int(feature, default):
            try: return int(_gv(feature, str(default)))
            except (ValueError, TypeError): return default

        # 'eq' must come from JSON state (driver's sysfs read returns only the
        # bands, no profile prefix — first token would be band 0, not profile).
        eq_state = (self._device.state.get('eq', '') if self._device else '').split()
        try: self._eq_target_profile = int(eq_state[0]) if eq_state else 0
        except ValueError: self._eq_target_profile = 0
        # Last active-preset value the *device* reported. Live-sync only moves
        # the green highlight when this actually changes (a real on-headset
        # switch), so clicking a profile in the GUI isn't reverted a poll later
        # by the device still reporting the previous active slot.
        self._last_eq_device_preset = -1
        # Power-save timeout in minutes. 0 = "Never sleep" (a valid choice, not
        # disabled state — the toggle was UX bloat that conflated this).
        self._pwr_timeout = _int('power_save', 30)
        self._thx_on = _gv('thx', '0') == '1'
        self._ull_on = _gv('ull', '1') == '1'
        anc_raw = _gv('anc', '0 1').split()
        self._anc_mode  = int(anc_raw[0]) if anc_raw and anc_raw[0].isdigit() else 0
        self._anc_level = int(anc_raw[1]) if len(anc_raw) > 1 and anc_raw[1].isdigit() else 1
        self._gc_balance     = _int('game_chat', 10)
        self._in_call_mix    = _int('in_call_mix', 0)
        self._audio_prompts  = _gv('audio_prompts', '1') == '1'
        self._sidetone_level = _int('sidetone', 0)
        self._mic_target_idx = _int('mic_eq_preset', 0)
        self._fn_mode        = _int('audio_fn_button', 1)

        # status refresh
        GLib.timeout_add(2000, self._refresh_status)
        # live sync: reflect on-board (headset button) changes in the UI.
        # Polls only the driver's cached attrs (no device query → no blocking),
        # so it's safe to run frequently on the wireless link.
        GLib.timeout_add(700, self._live_sync)

        nb = Gtk.Notebook()
        nb.set_tab_pos(Gtk.PositionType.TOP)
        self.set_child(nb)

        nb.append_page(self._build_sound_tab(), Gtk.Label(label='SOUND'))
        nb.append_page(self._build_enhancement_tab(), Gtk.Label(label='ENHANCEMENT'))
        nb.append_page(self._build_mic_tab(), Gtk.Label(label='MIC'))
        nb.append_page(self._build_power_tab(), Gtk.Label(label='POWER'))

        # Battery indicator parked at the right end of the tab row (V3 Pro only).
        self._bat_widget = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._bat_widget.set_margin_end(12)
        self._bat_widget.set_valign(Gtk.Align.CENTER)
        self._bat_label = Gtk.Label(label='')
        self._bat_label.add_css_class('value-label')
        self._bat_widget.append(self._bat_label)
        nb.set_action_widget(self._bat_widget, Gtk.PackType.END)
        self._refresh_battery_widget()

        self.set_title(f'{self._device.name} Control' if self._device
                       else 'BlackShark Control')
        self._refresh_status()
        # Load the cached EQ preset into sliders on startup.
        GLib.idle_add(self._load_cached_preset)
        # Push cached mic-EQ band values into the slider widgets too. Skip the
        # write-back to the device so we don't re-apply on every launch.
        if self._has('mic_eq'):
            GLib.idle_add(lambda: (self._load_mic_sliders(self._mic_eq_values), False)[1])

    def _has(self, feature):
        return self._device is not None and self._device.has(feature)

    def _write_sync(self, feature, value):
        """Blocking sysfs write. ONLY use from worker threads — sysfs writes
        can take >1 s when the driver send path waits for an int-IN reply
        the V3 wireless firmware never delivers."""
        if not self._device:
            return False, 'device not found'
        return self._device.write(feature, value)

    def _write(self, feature, value, on_done=None):
        """Non-blocking sysfs write — runs on a daemon thread so the GTK main
        thread never freezes. Most handlers don't need the result; pass
        on_done(ok, err) if you do (callback fires via GLib.idle_add)."""
        if not self._device:
            if on_done:
                GLib.idle_add(lambda: (on_done(False, 'device not found'), False)[1])
            return
        import threading
        def _worker():
            ok, err = self._device.write(feature, value)
            if on_done:
                GLib.idle_add(lambda: (on_done(ok, err), False)[1])
        threading.Thread(target=_worker, daemon=True).start()

    def _read(self, feature):
        return self._device.read(feature) if self._device else None

    def _load_cached_preset(self):
        # Find the preset name corresponding to the cached profile index.
        name = next((n for n, i in PRESET_IDX.items() if i == self._eq_target_profile),
                    'Default')
        self._on_preset(self._preset_btns[name], name)
        return False

    # ── status bar ──────────────────────────────────────────────────────────

    def _status_widget(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._status_label = Gtk.Label(label='Detecting device…')
        self._status_label.add_css_class('status-ok')
        box.append(self._status_label)
        return box

    def _refresh_status(self):
        # Re-detect on each tick — handles unplug/replug.
        new_dev = Device.detect()
        if new_dev and (self._device is None or new_dev.pid != self._device.pid
                        or new_dev.path != self._device.path):
            self._device = new_dev
        elif new_dev is None:
            self._device = None

        if self._device:
            # Show device id immediately — battery extras are appended later
            # by the async worker so we don't block the GTK main thread on
            # sysfs reads (each can take up to 500ms in the kernel; sync
            # reads here triggered GTK 'app-not-responding' on connect).
            self._status_label.set_text(
                f'{self._device.name} · {self._device.pid} · '
                f'{os.path.basename(self._device.path)}'
            )
            if self._device.has('battery') and not getattr(self, '_status_bat_inflight', False):
                self._status_bat_inflight = True
                dev = self._device
                def _bat_worker():
                    bl = dev.read('battery')
                    ch = dev.read('charging')
                    GLib.idle_add(self._status_apply_battery, dev, bl, ch)
                threading.Thread(target=_bat_worker, daemon=True).start()
            self._status_label.remove_css_class('status-err')
            self._status_label.add_css_class('status-ok')
            self._connected_pid = self._device.pid
        else:
            self._status_label.set_text('Device not found — plug in headset/dongle')
            self._status_label.remove_css_class('status-ok')
            self._status_label.add_css_class('status-err')
            self._connected_pid = None
        self._refresh_battery_widget()
        return True   # keep timer running

    def _status_apply_battery(self, dev, bl, ch):
        """Called from the worker thread via GLib.idle_add — runs on main."""
        self._status_bat_inflight = False
        # Bail if device disconnected/replaced while the worker was running.
        if self._device is None or self._device is not dev:
            return False
        # Local-only: drop the bl != '-1' filter so we always show whatever
        # the driver reports, even when it's -1. Helps see if anything is
        # ever returning real data on V3 wireless.
        extras = ''
        if bl:
            try:
                pct = round(int(bl) / 255 * 100)
                extras = f' · battery {pct}%'
                if ch == '1':
                    extras += ' (charging)'
            except ValueError:
                pass
        self._status_label.set_text(
            f'{dev.name} · {dev.pid} · {os.path.basename(dev.path)}{extras}'
        )
        return False  # one-shot

    # ── live sync (on-board button changes → UI) ─────────────────────────────

    # Only cache-backed attrs are polled here. Reading these returns the value
    # the driver's raw_event cached from the headset's spontaneous pushes — no
    # HID command is sent, so it never disturbs the wireless link. thx/ull/
    # battery are intentionally excluded: reading them issues a blocking GET.
    _SYNC_FEATURES = ('sidetone', 'game_chat', 'in_call_mix', 'audio_fn_button',
                      'audio_prompts', 'mic_eq_preset', 'eq')

    @staticmethod
    def _sync_int(raw):
        """First token of a sysfs value as int, or None if unset ('-1')/garbage."""
        if raw is None:
            return None
        tok = raw.split()
        if not tok:
            return None
        try:
            v = int(tok[0])
        except ValueError:
            return None
        return None if v < 0 else v

    def _live_sync(self):
        if not self._device or self._sync_inflight:
            return True
        self._sync_inflight = True
        dev = self._device
        def _worker():
            vals = {f: dev.read(f) for f in self._SYNC_FEATURES if dev.has(f)}
            GLib.idle_add(self._apply_live_sync, dev, vals)
        threading.Thread(target=_worker, daemon=True).start()
        return True   # keep timer running

    def _apply_live_sync(self, dev, vals):
        self._sync_inflight = False
        if self._device is not dev:
            return False
        self._syncing = True
        try:
            v = self._sync_int(vals.get('sidetone'))
            if v is not None and getattr(self, '_sidetone_slider', None) and v != self._sidetone_level:
                self._sidetone_level = v
                self._sidetone_slider.set_value(v)

            v = self._sync_int(vals.get('game_chat'))
            if v is not None and getattr(self, '_gc_slider', None) and v != self._gc_balance:
                self._gc_balance = v
                self._gc_slider.set_value(v)

            v = self._sync_int(vals.get('in_call_mix'))
            if v is not None and v != self._in_call_mix and v in getattr(self, '_ic_btns', {}):
                self._in_call_mix = v
                for b in self._ic_btns.values():
                    b.remove_css_class('active')
                self._ic_btns[v].add_css_class('active')

            v = self._sync_int(vals.get('audio_fn_button'))
            if v is not None and v != self._fn_mode and v in getattr(self, '_fn_btns', {}):
                self._fn_mode = v
                for b in self._fn_btns.values():
                    b.remove_css_class('active')
                self._fn_btns[v].add_css_class('active')

            v = self._sync_int(vals.get('audio_prompts'))
            if v is not None and getattr(self, '_ap_btn', None) and (v == 1) != self._audio_prompts:
                self._audio_prompts = (v == 1)
                self._ap_btn.set_label('ON' if self._audio_prompts else 'OFF')
                self._ap_btn.remove_css_class('toggle-off' if self._audio_prompts else 'toggle-on')
                self._ap_btn.add_css_class('toggle-on' if self._audio_prompts else 'toggle-off')

            v = self._sync_int(vals.get('mic_eq_preset'))
            if v is not None and v != self._mic_target_idx and v in getattr(self, '_mic_preset_btns', {}):
                self._sync_mic_preset(v)

            v = self._sync_int(vals.get('eq'))
            if v is not None and v != self._last_eq_device_preset:
                # Only react to a genuine device-side change, not to the device
                # echoing back the slot we just wrote.
                self._last_eq_device_preset = v
                if v != self._eq_target_profile:
                    self._sync_eq_preset(v)
        finally:
            self._syncing = False
        return False   # one-shot (idle_add)

    def _sync_eq_preset(self, idx):
        """Mirror an on-board headphone-EQ preset switch. Highlights the preset
        and loads its bands into the sliders WITHOUT writing back to the device."""
        name = next((n for n, i in PRESET_IDX.items() if i == idx), None)
        if name is None:
            return   # custom slot with no preset button — nothing to highlight
        for b in self._preset_btns.values():
            b.remove_css_class('active')
        self._preset_btns[name].add_css_class('active')
        self._eq_target_profile = idx
        self._eq_preset_cleared = False
        self._update_profile_selector()
        self._load_sliders(self._eq_custom.get(idx, list(EQ_FACTORY[idx])))
        self._update_hex_preview()

    def _sync_mic_preset(self, idx):
        """Mirror an on-board mic-EQ preset switch (highlight + load bands)."""
        for b in self._mic_preset_btns.values():
            b.remove_css_class('active')
        self._mic_preset_btns[idx].add_css_class('active')
        self._mic_target_idx = idx
        self._load_mic_sliders(self._mic_eq_custom.get(idx, list(MIC_EQ_FACTORY[idx])))

    # ── Sound tab ───────────────────────────────────────────────────────────

    def _build_sound_tab(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(16); outer.set_margin_bottom(16)
        outer.set_margin_start(20); outer.set_margin_end(20)
        outer.add_css_class('main-box')

        # status
        status_box = self._status_widget()
        outer.append(status_box)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        hbox.set_margin_top(12)
        outer.append(hbox)

        # left: THX + presets
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left.set_size_request(200, -1)
        hbox.append(left)

        # THX toggle
        thx_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        thx_card.add_css_class('card')
        thx_lbl = Gtk.Label(label='THX SPATIAL AUDIO')
        thx_lbl.add_css_class('section-label')
        thx_lbl.set_halign(Gtk.Align.START)
        thx_card.append(thx_lbl)
        thx_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._thx_btn = Gtk.Button(label='THX' if self._thx_on else 'STEREO')
        self._thx_btn.add_css_class('toggle-on' if self._thx_on else 'toggle-off')
        self._thx_btn.connect('clicked', self._on_thx_toggle)
        thx_row.append(self._thx_btn)
        thx_card.append(thx_row)
        left.append(thx_card)

        # presets
        preset_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preset_card.add_css_class('card')
        preset_lbl = Gtk.Label(label='AUDIO EQUALIZER')
        preset_lbl.add_css_class('section-label')
        preset_lbl.set_halign(Gtk.Align.START)
        preset_card.append(preset_lbl)

        self._preset_btns = {}
        for name in EQ_PRESETS:
            btn = Gtk.Button(label=name)
            btn.add_css_class('preset-btn')
            btn.connect('clicked', self._on_preset, name)
            preset_card.append(btn)
            self._preset_btns[name] = btn
        left.append(preset_card)

        # right: EQ sliders
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_hexpand(True)
        hbox.append(right)

        eq_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        eq_card.add_css_class('card')
        eq_card.set_hexpand(True)

        eq_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        eq_h = Gtk.Label(label='EQUALIZER  (-6 dB … +6 dB)')
        eq_h.add_css_class('section-label')
        eq_h.set_halign(Gtk.Align.START)
        eq_top.append(eq_h)

        # db labels on right side
        db_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        db_col.set_valign(Gtk.Align.CENTER)
        for db in ('+6', '+3', '0', '-3', '-6'):
            l = Gtk.Label(label=db)
            l.add_css_class('db-label')
            l.set_size_request(-1, 28)
            db_col.append(l)

        eq_card.append(eq_top)

        slider_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        slider_row.set_hexpand(True)

        self._eq_sliders = []
        self._eq_val_labels = []
        for i, freq in enumerate(EQ_FREQS):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            col.set_hexpand(True)

            val_lbl = Gtk.Label(label='0')
            val_lbl.add_css_class('value-label')
            val_lbl.set_size_request(40, -1)

            adj = Gtk.Adjustment(value=0, lower=-6, upper=6, step_increment=1, page_increment=1)
            sl = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, adjustment=adj)
            sl.set_inverted(True)  # top = +6
            sl.set_draw_value(False)
            sl.set_size_request(40, 130)
            sl.add_mark(0, Gtk.PositionType.RIGHT, None)
            sl.connect('value-changed', self._on_eq_slider, i)

            freq_lbl = Gtk.Label(label=freq)
            freq_lbl.add_css_class('freq-label')

            col.append(val_lbl)
            col.append(sl)
            col.append(freq_lbl)
            slider_row.append(col)

            self._eq_sliders.append(sl)
            self._eq_val_labels.append(val_lbl)

        eq_card.append(slider_row)

        # hex preview
        self._hex_label = Gtk.Label(label='hex: (not connected)')
        self._hex_label.add_css_class('hex-view')
        self._hex_label.set_halign(Gtk.Align.START)
        self._hex_label.set_selectable(True)
        eq_card.append(self._hex_label)

        # reset to factory defaults button (bottom middle)
        reset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        reset_row.set_halign(Gtk.Align.CENTER)
        reset_row.set_margin_top(4)
        reset_btn = Gtk.Button(label='Reset to Default Values')
        reset_btn.add_css_class('preset-btn')
        reset_btn.connect('clicked', self._on_reset_to_factory)
        reset_row.append(reset_btn)
        eq_card.append(reset_row)

        # profile target selector
        prof_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        prof_row.set_margin_top(4)
        prof_lbl = Gtk.Label(label='Write to:')
        prof_lbl.add_css_class('db-label')
        prof_row.append(prof_lbl)
        self._profile_sel_btns = {}
        for name, idx in PRESET_IDX.items():
            b = Gtk.Button(label=name)
            b.add_css_class('pwr-btn')
            if idx == 0:
                b.add_css_class('active')
            b.connect('clicked', self._on_profile_sel, idx)
            prof_row.append(b)
            self._profile_sel_btns[idx] = b
        eq_card.append(prof_row)

        apply_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        apply_row.set_halign(Gtk.Align.END)
        apply_row.set_margin_top(4)
        apply_btn = Gtk.Button(label='Apply EQ')
        apply_btn.add_css_class('apply-btn')
        apply_btn.connect('clicked', self._on_eq_apply)
        apply_row.append(apply_btn)
        eq_card.append(apply_row)

        right.append(eq_card)

        # Render the initial hex preview now that the label exists — otherwise
        # the placeholder "hex: (not connected)" stays visible until the user
        # clicks a preset or moves a slider.
        self._update_hex_preview()

        return outer

    def _on_thx_toggle(self, btn):
        self._thx_on = not self._thx_on
        if self._thx_on:
            btn.set_label('THX SPATIAL AUDIO')
            btn.remove_css_class('toggle-off')
            btn.add_css_class('toggle-on')
        else:
            btn.set_label('STEREO')
            btn.remove_css_class('toggle-on')
            btn.add_css_class('toggle-off')
        self._write('thx', '1' if self._thx_on else '0')

    def _on_preset(self, btn, name):
        # Cancel any pending debounce timer SYNCHRONOUSLY — before we change
        # _eq_target_profile, so a stale slider-drag debounce can't fire with
        # the new profile but old band values (which would write Game's bands
        # to slot 2 when switching Game → Movie).
        if self._eq_apply_timer:
            GLib.source_remove(self._eq_apply_timer)
            self._eq_apply_timer = None

        # Update button highlight first and let GTK paint it before doing
        # anything else — the slider-load + sysfs-write takes long enough
        # that without the deferral the previous preset's green styling
        # appeared to linger for ~1s before the new selection lit up.
        for n, b in self._preset_btns.items():
            b.remove_css_class('active')
        btn.add_css_class('active')
        self._eq_target_profile = PRESET_IDX[name]
        self._eq_preset_cleared = False  # reset for next drag
        self._update_profile_selector()

        def _finish_preset_change():
            vals = self._eq_custom.get(self._eq_target_profile,
                                       list(EQ_FACTORY[self._eq_target_profile]))
            self._load_sliders(vals)
            self._eq_apply_timer = GLib.timeout_add(50, self._debounced_apply)
            return False

        GLib.idle_add(_finish_preset_change)

    def _load_sliders(self, vals):
        self._ignore_slider = True
        for i, sl in enumerate(self._eq_sliders):
            sl.set_value(vals[i])
            self._eq_val_labels[i].set_text(f'{vals[i]:+d}' if vals[i] != 0 else '0')
        self._ignore_slider = False
        self._eq_values = list(vals)

    def _on_reset_to_factory(self, btn):
        name = [n for n, i in PRESET_IDX.items() if i == self._eq_target_profile][0]
        self._load_sliders(list(EQ_FACTORY[self._eq_target_profile]))
        if self._eq_apply_timer:
            GLib.source_remove(self._eq_apply_timer)
        self._eq_apply_timer = GLib.timeout_add(50, self._debounced_apply)

    def _on_eq_slider(self, sl, idx):
        if self._ignore_slider:
            return
        v = int(round(sl.get_value()))
        if self._eq_values[idx] == v:
            # value-changed fires on every sub-pixel; skip if int value unchanged
            return
        self._eq_values[idx] = v
        self._eq_val_labels[idx].set_text(f'{v:+d}' if v != 0 else '0')
        # Clear preset highlights only the FIRST time during a drag —
        # iterating + removing css class on 5 buttons triggers re-layout
        # and adds visible lag when fired hundreds of times per second.
        if not getattr(self, '_eq_preset_cleared', False):
            for b in self._preset_btns.values():
                b.remove_css_class('active')
            self._eq_preset_cleared = True
        # Coalesce hex preview updates — let GTK redraw at its own rate.
        if not getattr(self, '_eq_hex_pending', False):
            self._eq_hex_pending = True
            def _redraw():
                self._eq_hex_pending = False
                self._update_hex_preview()
                return False
            GLib.idle_add(_redraw)
        if self._eq_apply_timer:
            GLib.source_remove(self._eq_apply_timer)
        self._eq_apply_timer = GLib.timeout_add(300, self._debounced_apply)

    def _debounced_apply(self):
        self._eq_apply_timer = None
        self._apply_eq()
        return False

    def _update_hex_preview(self, status=None):
        buf = bytearray(64)
        buf[0] = 0x02
        buf[2] = 0x60
        buf[6] = 0x0f
        buf[9] = 0x80 if getattr(self, '_connected_pid', None) != '0579' else 0x00
        buf[10] = 0x95
        buf[12] = 0x0b
        buf[13] = self._eq_target_profile
        for i, v in enumerate(self._eq_values):
            buf[14 + i] = (0x80 | (-v)) if v < 0 else v
        crc = 0
        for b in buf[:62]:
            crc ^= b
        buf[62] = crc
        hex_str = ' '.join(f'{b:02x}' for b in buf[:24]) + ' ...'
        sysfs_cmd = f"{self._eq_target_profile} " + ' '.join(str(v) for v in self._eq_values)
        idx = self._eq_target_profile
        pname = PROFILE_NAMES[idx] if 0 <= idx < len(PROFILE_NAMES) else f'slot {idx}'
        lines = [
            f'0x95 cmd (profile={self._eq_target_profile} {pname}): {hex_str}',
            f'bands[0..9]: {" ".join(str(v) for v in self._eq_values)}',
            f'sysfs: "{sysfs_cmd}"',
        ]
        if status:
            lines.append(status)
        self._hex_label.set_text('\n'.join(lines))

    def _update_mic_hex_preview(self, status=None):
        if not hasattr(self, '_mic_hex_label'):
            return
        # Mic EQ uses cmd 0x97 with 10 sign-magnitude band gains at buf[13..22].
        # Frequencies 31/63/125/250/500/1k/2k/4k/8k/16k Hz.
        buf = bytearray(64)
        buf[0] = 0x02
        buf[2] = 0x60
        buf[6] = 0x0e
        buf[9] = 0x80 if getattr(self, '_connected_pid', None) != '0579' else 0x00
        buf[10] = 0x97
        buf[12] = 0x0a
        for i, v in enumerate(self._mic_eq_values):
            buf[13 + i] = (0x80 | (-v)) if v < 0 else v
        crc = 0
        for b in buf[:62]:
            crc ^= b
        buf[62] = crc
        hex_str = ' '.join(f'{b:02x}' for b in buf[:24]) + ' ...'
        sysfs_cmd = ' '.join(str(v) for v in self._mic_eq_values)
        preset_name = MIC_EQ_PRESETS[self._mic_target_idx] if 0 <= self._mic_target_idx < len(MIC_EQ_PRESETS) else '?'
        lines = [
            f'0x97 cmd (preset={self._mic_target_idx} {preset_name}): {hex_str}',
            f'bands[0..9]: {" ".join(str(v) for v in self._mic_eq_values)}',
            f'sysfs: "{sysfs_cmd}"',
        ]
        if status:
            lines.append(status)
        self._mic_hex_label.set_text('\n'.join(lines))

    def _update_profile_selector(self):
        for idx, btn in self._profile_sel_btns.items():
            if idx == self._eq_target_profile:
                btn.add_css_class('active')
            else:
                btn.remove_css_class('active')

    def _on_profile_sel(self, btn, idx):
        # treat exactly like clicking the matching preset button
        name = [n for n, i in PRESET_IDX.items() if i == idx][0]
        self._on_preset(self._preset_btns[name], name)

    def _apply_eq(self, profile_idx=None):
        if profile_idx is None:
            profile_idx = self._eq_target_profile
        val_str = f"{profile_idx} " + ' '.join(str(v) for v in self._eq_values)
        snapshot_vals = list(self._eq_values)
        pname = PROFILE_NAMES[profile_idx] if 0 <= profile_idx < len(PROFILE_NAMES) else f'slot {profile_idx}'

        # Run the sysfs write on a worker thread — the driver's 5-step HID
        # sequence blocks the caller for ~750ms (4 inter-write msleep(150)).
        # Doing it on the GTK main thread froze paint, which made the new
        # preset's green styling appear ~1s late.
        # On V3 Pro the EQ sysfs only takes a slot index — sliders are advisory.
        write_val = (str(profile_idx) if self._device and self._device.caps.get('eq_mode') == 'slot-only'
                     else val_str)
        def _worker():
            ok, err = self._write_sync('eq', write_val)
            def _on_done():
                if ok:
                    self._eq_custom[profile_idx] = snapshot_vals
                    save_eq_config(self._eq_custom)
                    status = f'→ Written to slot {profile_idx} ({pname})'
                else:
                    status = f'→ Write failed: {err}'
                self._update_hex_preview(status=status)
                return False
            GLib.idle_add(_on_done)

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _on_eq_apply(self, btn):
        self._apply_eq()

    # ── Enhancement tab ─────────────────────────────────────────────────────

    def _build_enhancement_tab(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_margin_top(16); outer.set_margin_bottom(16)
        outer.set_margin_start(20); outer.set_margin_end(20)
        outer.add_css_class('main-box')

        # ULL — wireless only
        if self._has('ull'):
            ull_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            ull_card.add_css_class('card')
            ull_lbl = Gtk.Label(label='ULTRA-LOW LATENCY')
            ull_lbl.add_css_class('section-label')
            ull_lbl.set_halign(Gtk.Align.START)
            ull_card.append(ull_lbl)

            ull_desc = Gtk.Label(label='High-speed wireless audio via Razer HyperSpeed Gen-2 dongle.\nEnabled: ~10ms latency.  Disabled: extended range & battery life.')
            ull_desc.set_wrap(True)
            ull_desc.set_halign(Gtk.Align.START)
            ull_desc.set_xalign(0)
            ull_card.append(ull_desc)

            self._ull_btn = Gtk.Button(label='ON' if self._ull_on else 'OFF')
            self._ull_btn.add_css_class('toggle-on' if self._ull_on else 'toggle-off')
            self._ull_btn.set_halign(Gtk.Align.START)
            self._ull_btn.connect('clicked', self._on_ull_toggle)
            ull_card.append(self._ull_btn)
            outer.append(ull_card)

        # ANC + Ambient — V3 Pro only
        if self._has('anc'):
            anc_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            anc_card.add_css_class('card')
            anc_lbl = Gtk.Label(label='ACTIVE NOISE CANCELLATION')
            anc_lbl.add_css_class('section-label')
            anc_lbl.set_halign(Gtk.Align.START)
            anc_card.append(anc_lbl)

            anc_mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self._anc_mode_btns = {}
            for mode_val, mode_name in [(0, 'Off'), (1, 'ANC'), (2, 'Ambient')]:
                b = Gtk.Button(label=mode_name)
                b.add_css_class('pwr-btn')
                if mode_val == self._anc_mode:
                    b.add_css_class('active')
                b.connect('clicked', self._on_anc_mode, mode_val)
                anc_mode_row.append(b)
                self._anc_mode_btns[mode_val] = b
            anc_card.append(anc_mode_row)

            anc_lvl_lbl = Gtk.Label(label='ANC level (1–4, ANC mode only)')
            anc_lvl_lbl.set_halign(Gtk.Align.START); anc_lvl_lbl.set_xalign(0)
            anc_card.append(anc_lvl_lbl)
            anc_lvl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self._anc_lvl_btns = {}
            for lvl in (1, 2, 3, 4):
                b = Gtk.Button(label=str(lvl))
                b.add_css_class('pwr-btn')
                if lvl == self._anc_level:
                    b.add_css_class('active')
                b.set_sensitive(self._anc_mode == 1)
                b.connect('clicked', self._on_anc_level, lvl)
                anc_lvl_row.append(b)
                self._anc_lvl_btns[lvl] = b
            anc_card.append(anc_lvl_row)
            outer.append(anc_card)

        # Game/Chat balance
        gc_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        gc_card.add_css_class('card')
        gc_lbl = Gtk.Label(label='GAME / CHAT BALANCE')
        gc_lbl.add_css_class('section-label')
        gc_lbl.set_halign(Gtk.Align.START)
        gc_card.append(gc_lbl)
        gc_desc = Gtk.Label(label='0 = full game · 10 = center · 20 = full chat')
        gc_desc.set_halign(Gtk.Align.START)
        gc_desc.set_xalign(0)
        gc_card.append(gc_desc)
        gc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._gc_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 20, 1)
        self._gc_slider.set_value(self._gc_balance)
        self._gc_slider.set_hexpand(True)
        self._gc_slider.set_draw_value(True)
        self._gc_slider.connect('value-changed', self._on_game_chat_balance)
        gc_row.append(self._gc_slider)
        gc_card.append(gc_row)
        outer.append(gc_card)

        # In-call audio mix
        ic_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        ic_card.add_css_class('card')
        ic_lbl = Gtk.Label(label='IN-CALL AUDIO MIX')
        ic_lbl.add_css_class('section-label')
        ic_lbl.set_halign(Gtk.Align.START)
        ic_card.append(ic_lbl)
        ic_desc = Gtk.Label(label='What happens to 2.4 GHz game audio when a Bluetooth call comes in.')
        ic_desc.set_halign(Gtk.Align.START)
        ic_desc.set_xalign(0)
        ic_card.append(ic_desc)
        ic_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._ic_btns = {}
        for mode_val, mode_name in [(0, 'Combine 2.4 + BT'), (1, 'Lower 2.4'), (2, 'Mute 2.4')]:
            b = Gtk.Button(label=mode_name)
            b.add_css_class('pwr-btn')
            b.connect('clicked', self._on_in_call_mix, mode_val)
            ic_row.append(b)
            self._ic_btns[mode_val] = b
        self._ic_btns[self._in_call_mix].add_css_class('active')
        ic_card.append(ic_row)
        outer.append(ic_card)

        return outer

    def _on_ull_toggle(self, btn):
        self._ull_on = not self._ull_on
        if self._ull_on:
            btn.set_label('ON')
            btn.remove_css_class('toggle-off')
            btn.add_css_class('toggle-on')
        else:
            btn.set_label('OFF')
            btn.remove_css_class('toggle-on')
            btn.add_css_class('toggle-off')
        self._write('ull', '1' if self._ull_on else '0')

    def _on_game_chat_balance(self, sl):
        if self._syncing:
            return   # mirroring an on-board change — don't echo it back
        val = int(round(sl.get_value()))
        self._write('game_chat', str(val))

    def _on_in_call_mix(self, btn, mode):
        for b in self._ic_btns.values():
            b.remove_css_class('active')
        btn.add_css_class('active')
        self._write('in_call_mix', str(mode))

    def _on_anc_mode(self, btn, mode):
        for b in self._anc_mode_btns.values():
            b.remove_css_class('active')
        btn.add_css_class('active')
        self._anc_mode = mode
        # Level only applies to ANC mode — Off and Ambient ignore it.
        for lvl_btn in self._anc_lvl_btns.values():
            lvl_btn.set_sensitive(mode == 1)
        self._write('anc', f'{mode} {self._anc_level}')

    def _on_anc_level(self, btn, lvl):
        for b in self._anc_lvl_btns.values():
            b.remove_css_class('active')
        btn.add_css_class('active')
        self._anc_level = lvl
        # Only re-write if in ANC mode (level is irrelevant otherwise)
        if self._anc_mode == 1:
            self._write('anc', f'1 {lvl}')

    # ── Mic tab ─────────────────────────────────────────────────────────────

    def _build_mic_tab(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(16); outer.set_margin_bottom(16)
        outer.set_margin_start(20); outer.set_margin_end(20)
        outer.add_css_class('main-box')

        # Mic volume note (UAC2 standard, not Razer-specific)
        vol_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vol_card.add_css_class('card')
        vol_lbl = Gtk.Label(label='MICROPHONE VOLUME')
        vol_lbl.add_css_class('section-label')
        vol_lbl.set_halign(Gtk.Align.START)
        vol_card.append(vol_lbl)
        vol_note = Gtk.Label(label='Mic volume is standard USB Audio Class — control via PipeWire/pavucontrol\n'
                                    '(it\'s NOT a Razer-specific HID command).')
        vol_note.set_halign(Gtk.Align.START); vol_note.set_xalign(0)
        vol_card.append(vol_note)
        outer.append(vol_card)

        # Sidetone slider (0-15)
        st_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        st_card.add_css_class('card')
        st_lbl = Gtk.Label(label='SIDE TONE  (mic monitoring 0–15)')
        st_lbl.add_css_class('section-label')
        st_lbl.set_halign(Gtk.Align.START)
        st_card.append(st_lbl)
        st_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        st_lbl0 = Gtk.Label(label='0'); st_lbl0.add_css_class('db-label')
        st_adj = Gtk.Adjustment(value=self._sidetone_level, lower=0, upper=15, step_increment=1, page_increment=1)
        self._sidetone_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=st_adj)
        self._sidetone_slider.set_hexpand(True)
        self._sidetone_slider.set_draw_value(True)
        self._sidetone_slider.set_digits(0)
        self._sidetone_slider.connect('value-changed', self._on_sidetone)
        st_lbl15 = Gtk.Label(label='15'); st_lbl15.add_css_class('db-label')
        st_row.append(st_lbl0); st_row.append(self._sidetone_slider); st_row.append(st_lbl15)
        st_card.append(st_row)
        outer.append(st_card)

        # Mic EQ presets — V3 only (V3 Pro driver doesn't expose mic EQ yet)
        self._mic_preset_btns = {}
        self._mic_eq_sliders = []
        self._mic_eq_val_labels = []
        self._mic_eq_values = [0] * 10
        self._mic_ignore_slider = False
        self._mic_eq_apply_timer = None
        if self._has('mic_eq_preset'):
            mp_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            mp_card.add_css_class('card')
            mp_lbl = Gtk.Label(label='MIC EQ PRESET')
            mp_lbl.add_css_class('section-label')
            mp_lbl.set_halign(Gtk.Align.START)
            mp_card.append(mp_lbl)
            mp_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for i, name in enumerate(MIC_EQ_PRESETS):
                b = Gtk.Button(label=name)
                b.add_css_class('pwr-btn')
                if i == self._mic_target_idx:
                    b.add_css_class('active')
                b.connect('clicked', self._on_mic_preset, i)
                mp_row.append(b)
                self._mic_preset_btns[i] = b
            # Restore the cached preset's bands into the sliders.
            self._mic_eq_values = list(self._mic_eq_custom.get(
                self._mic_target_idx, MIC_EQ_FACTORY[self._mic_target_idx]))
            mp_card.append(mp_row)
            outer.append(mp_card)

        # Mic EQ 10-band sliders — V3 only
        if self._has('mic_eq'):
            meq_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            meq_card.add_css_class('card')
            meq_lbl = Gtk.Label(label='MIC EQUALIZER  (-6 dB … +6 dB)')
            meq_lbl.add_css_class('section-label')
            meq_lbl.set_halign(Gtk.Align.START)
            meq_card.append(meq_lbl)

            meq_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            meq_row.set_hexpand(True)
            for i, freq in enumerate(EQ_FREQS):
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                col.set_hexpand(True)
                val_lbl = Gtk.Label(label='0')
                val_lbl.add_css_class('value-label')
                adj = Gtk.Adjustment(value=0, lower=-6, upper=6, step_increment=1, page_increment=1)
                sl = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, adjustment=adj)
                sl.set_inverted(True); sl.set_draw_value(False)
                sl.set_size_request(40, 140)
                sl.add_mark(0, Gtk.PositionType.RIGHT, None)
                sl.connect('value-changed', self._on_mic_eq_slider, i)
                freq_lbl = Gtk.Label(label=freq); freq_lbl.add_css_class('freq-label')
                col.append(val_lbl); col.append(sl); col.append(freq_lbl)
                meq_row.append(col)
                self._mic_eq_sliders.append(sl)
                self._mic_eq_val_labels.append(val_lbl)
            meq_card.append(meq_row)

            # Bottom row: hex preview on the left, reset button on the right.
            bottom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            bottom_row.set_margin_top(4)
            self._mic_hex_label = Gtk.Label(label='hex: (not connected)')
            self._mic_hex_label.add_css_class('hex-view')
            self._mic_hex_label.set_halign(Gtk.Align.START)
            self._mic_hex_label.set_hexpand(True)
            self._mic_hex_label.set_selectable(True)
            bottom_row.append(self._mic_hex_label)

            meq_reset_btn = Gtk.Button(label='Reset to Default Values')
            meq_reset_btn.add_css_class('preset-btn')
            meq_reset_btn.set_valign(Gtk.Align.END)
            meq_reset_btn.connect('clicked', self._on_mic_reset_to_factory)
            bottom_row.append(meq_reset_btn)
            meq_card.append(bottom_row)
            outer.append(meq_card)
            self._update_mic_hex_preview()

        # Audio function button mode — V3 only
        self._fn_btns = {}
        if self._has('audio_fn_button'):
            fn_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            fn_card.add_css_class('card')
            fn_lbl = Gtk.Label(label='AUDIO FUNCTION BUTTON')
            fn_lbl.add_css_class('section-label')
            fn_lbl.set_halign(Gtk.Align.START)
            fn_card.append(fn_lbl)
            fn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            # All 4 bytes verified on-device 2026-05-03 after widening the
            # driver's clamp from 1..2 to 0..255. Earlier "sidetone == bluetooth"
            # confusion was the driver rejecting byte 3 with -EINVAL.
            for mode_val, mode_name in [
                (0, 'Game/Chat'),
                (1, 'Sidetone Save'),
                (2, 'Footsteps Scaling'),
                (3, 'Bluetooth Volume'),
            ]:
                b = Gtk.Button(label=mode_name)
                b.add_css_class('pwr-btn')
                if mode_val == self._fn_mode:
                    b.add_css_class('active')
                b.connect('clicked', self._on_fn_button, mode_val)
                fn_row.append(b)
                self._fn_btns[mode_val] = b
            fn_card.append(fn_row)
            outer.append(fn_card)

        # Audio prompts toggle
        ap_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        ap_card.add_css_class('card')
        ap_lbl = Gtk.Label(label='AUDIO PROMPTS')
        ap_lbl.add_css_class('section-label')
        ap_lbl.set_halign(Gtk.Align.START)
        ap_card.append(ap_lbl)
        ap_desc = Gtk.Label(label='Voice prompts for mic mute/unmute events.')
        ap_desc.set_halign(Gtk.Align.START)
        ap_desc.set_xalign(0)
        ap_card.append(ap_desc)
        self._ap_btn = Gtk.Button(label='ON' if self._audio_prompts else 'OFF')
        self._ap_btn.add_css_class('toggle-on' if self._audio_prompts else 'toggle-off')
        self._ap_btn.set_halign(Gtk.Align.START)
        self._ap_btn.connect('clicked', self._on_audio_prompts)
        ap_card.append(self._ap_btn)
        outer.append(ap_card)

        return outer

    def _on_audio_prompts(self, btn):
        self._audio_prompts = not self._audio_prompts
        if self._audio_prompts:
            btn.set_label('ON')
            btn.remove_css_class('toggle-off')
            btn.add_css_class('toggle-on')
        else:
            btn.set_label('OFF')
            btn.remove_css_class('toggle-on')
            btn.add_css_class('toggle-off')
        self._write('audio_prompts', '1' if self._audio_prompts else '0')

    def _on_sidetone(self, sl):
        if self._syncing:
            return   # mirroring an on-board change — don't echo it back
        val = int(round(sl.get_value()))
        self._write('sidetone', str(val))

    def _load_mic_sliders(self, vals):
        self._mic_ignore_slider = True
        for i, sl in enumerate(self._mic_eq_sliders):
            sl.set_value(vals[i])
            self._mic_eq_val_labels[i].set_text(f'{vals[i]:+d}' if vals[i] != 0 else '0')
        self._mic_ignore_slider = False
        self._mic_eq_values = list(vals)
        self._update_mic_hex_preview()

    def _apply_mic_eq(self, idx=None):
        if idx is None:
            idx = self._mic_target_idx
        # Optimistically update local state + status; sysfs writes happen on
        # worker threads so the GTK main thread doesn't freeze.
        self._mic_eq_custom[idx] = list(self._mic_eq_values)
        save_mic_eq_config(self._mic_eq_custom)
        self._write('mic_eq', ' '.join(str(v) for v in self._mic_eq_values))
        self._write('mic_eq_preset', str(idx))
        self._update_mic_hex_preview(status=f'→ Written to mic preset {idx}')

    def _on_mic_preset(self, btn, idx):
        for b in self._mic_preset_btns.values():
            b.remove_css_class('active')
        btn.add_css_class('active')
        self._mic_target_idx = idx
        # load saved custom values for this slot (or factory if first time)
        vals = self._mic_eq_custom.get(idx, list(MIC_EQ_FACTORY[idx]))
        self._load_mic_sliders(vals)
        self._apply_mic_eq(idx)

    def _on_mic_eq_slider(self, sl, idx):
        if self._mic_ignore_slider:
            return
        v = int(round(sl.get_value()))
        if self._mic_eq_values[idx] == v:
            return
        self._mic_eq_values[idx] = v
        self._mic_eq_val_labels[idx].set_text(f'{v:+d}' if v != 0 else '0')
        # Coalesce hex preview redraws to one per idle tick
        if not getattr(self, '_mic_hex_pending', False):
            self._mic_hex_pending = True
            def _redraw():
                self._mic_hex_pending = False
                self._update_mic_hex_preview()
                return False
            GLib.idle_add(_redraw)
        if self._mic_eq_apply_timer:
            GLib.source_remove(self._mic_eq_apply_timer)
        self._mic_eq_apply_timer = GLib.timeout_add(300, self._mic_eq_debounced_apply)

    def _mic_eq_debounced_apply(self):
        self._mic_eq_apply_timer = None
        self._apply_mic_eq()
        return False

    def _on_mic_reset_to_factory(self, btn):
        self._load_mic_sliders(list(MIC_EQ_FACTORY[self._mic_target_idx]))
        self._apply_mic_eq()

    def _on_fn_button(self, btn, mode):
        for b in self._fn_btns.values():
            b.remove_css_class('active')
        btn.add_css_class('active')
        self._write('audio_fn_button', str(mode))

    # ── Power tab ───────────────────────────────────────────────────────────

    def _build_power_tab(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_margin_top(16); outer.set_margin_bottom(16)
        outer.set_margin_start(20); outer.set_margin_end(20)
        outer.add_css_class('main-box')

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        outer.append(hbox)

        # left: wireless power save (only on wireless variants)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        hbox.append(left)

        self._timeout_btns = {}
        if self._has('power_save'):
            pwr_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            pwr_card.add_css_class('card')
            pwr_card.set_size_request(340, -1)

            pwr_lbl = Gtk.Label(label='WIRELESS POWER SAVING')
            pwr_lbl.add_css_class('section-label')
            pwr_lbl.set_halign(Gtk.Align.START)
            pwr_card.append(pwr_lbl)

            pwr_desc = Gtk.Label(label='Sleep after this many minutes of inactivity:')
            pwr_desc.set_halign(Gtk.Align.START)
            pwr_desc.set_xalign(0)
            pwr_card.append(pwr_desc)

            timeout_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for t, label in [(0, 'Never'), (15, '15'), (30, '30'), (45, '45'), (60, '60')]:
                btn = Gtk.Button(label=label)
                btn.add_css_class('pwr-btn')
                if t == self._pwr_timeout:
                    btn.add_css_class('active')
                btn.connect('clicked', self._on_timeout, t)
                timeout_row.append(btn)
                self._timeout_btns[t] = btn
            pwr_card.append(timeout_row)
            left.append(pwr_card)
        else:
            wired_note = Gtk.Label(label='Wireless power saving is only available\nwith the 2.4 GHz dongle or V3 Pro.')
            wired_note.set_halign(Gtk.Align.START); wired_note.set_xalign(0)
            wired_note.add_css_class('card')
            left.append(wired_note)

        # right: LED indicator (informational only)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hbox.append(right)

        led_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        led_card.add_css_class('card')
        led_card.set_size_request(300, -1)
        led_lbl = Gtk.Label(label='LED INDICATOR')
        led_lbl.add_css_class('section-label')
        led_lbl.set_halign(Gtk.Align.START)
        led_card.append(led_lbl)
        led_note = Gtk.Label(label='LED indicator mode (dongle) and other\npower commands not yet decoded.')
        led_note.set_halign(Gtk.Align.START)
        led_note.set_xalign(0)
        led_card.append(led_note)
        right.append(led_card)

        return outer

    def _refresh_battery_widget(self):
        """Update the battery indicator parked in the notebook's action area.
        Runs the sysfs reads on a worker thread because each can block ~2s when
        the firmware response gate is tripped (V3 wireless on Linux)."""
        if not hasattr(self, '_bat_widget'):
            return
        if not self._device:
            self._bat_label.set_text('battery —')
            return
        if not self._device.has('battery'):
            self._bat_label.set_text('battery N/A')
            return
        if getattr(self, '_bat_refresh_inflight', False):
            return  # don't pile up worker threads
        self._bat_refresh_inflight = True

        def _worker():
            bl = self._read('battery')
            ch = self._read('charging')
            GLib.idle_add(self._battery_update_done, bl, ch)
        threading.Thread(target=_worker, daemon=True).start()

    def _battery_update_done(self, bl, ch):
        self._bat_refresh_inflight = False
        try:
            raw = int(bl) if bl is not None else -1
        except ValueError:
            raw = -1
        if raw < 0:
            self._bat_label.set_text('battery —')
            return False
        pct = round(raw / 255 * 100)
        suffix = ' · charging' if ch == '1' else ''
        self._bat_label.set_text(f'battery {pct}%{suffix}')
        return False  # one-shot

    def _on_timeout(self, btn, t):
        for tb in self._timeout_btns.values():
            tb.remove_css_class('active')
        btn.add_css_class('active')
        self._pwr_timeout = t
        self._write('power_save', str(t))


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='com.blackshark.control')

    def do_activate(self):
        win = BlackSharkControl(self)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            win.get_display(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        win.present()


def main():
    app = App()
    return app.run()


if __name__ == '__main__':
    raise SystemExit(main())
