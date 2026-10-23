"""Shure Device & Audio Controller Backend
Handles WirePlumber / PipeWire state, live VU audio metering,
and full hardware state/control via shurectl.
"""

import json
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from typing import Callable, Optional


SHURECTL_BIN = shutil.which("shurectl") or os.path.expanduser("~/.local/bin/shurectl")


class ShureController:
    def __init__(self):
        self.device_name = "Shure MV6"
        self.firmware_version = ""
        self.serial_number = ""
        self.hid_path = ""
        self.is_connected = False
        self.has_hid_permission = False
        self.source_node_id: Optional[int] = None
        self.sink_node_id: Optional[int] = None
        self.is_muted = False
        self.volume = 1.0
        self.monitor_volume = 0.5
        
        # DSP settings (MV6)
        self.mode = "manual"  # "auto" or "manual"
        self.gain_db = 24.0   # 0 to 36 dB
        self.tone = 0         # -10 (Dark) to +10 (Bright)
        self.denoiser = True
        self.popper_stopper = True
        self.hpf = "75hz"     # "off", "75hz", "150hz"
        self.gain_locked = False
        self.mute_btn_disabled = False
        self.monitor_mix = 50
        
        # Live audio meter
        self.current_db = -60.0
        self.peak_db = -60.0
        self._peak_decay_time = time.time()
        self._meter_running = False
        self._meter_thread: Optional[threading.Thread] = None
        self._hotplug_thread: Optional[threading.Thread] = None
        self._meter_proc: Optional[subprocess.Popen] = None
        self._io_lock = threading.Lock()
        
        # Callbacks
        self.on_state_changed: Optional[Callable[[], None]] = None
        self.on_meter_update: Optional[Callable[[float, float], None]] = None
        
        # Initialize
        self.detect_devices()
        self.start_meter()
        self.start_hotplug_watcher()

    def _run_shurectl(self, args: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess:
        with self._io_lock:
            return subprocess.run(
                [SHURECTL_BIN] + args,
                capture_output=True,
                text=True,
                timeout=timeout
            )

    def _set_hw_async(self, key_val: str):
        """Send a hardware setting asynchronously to avoid blocking the GTK main loop."""
        if not self.has_hid_permission:
            return

        def worker():
            try:
                self._run_shurectl(["--set", key_val], timeout=3.0)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def detect_devices(self):
        """Detect Shure device via shurectl and PipeWire/WirePlumber."""
        was_connected = self.is_connected
        found_shure = False
        self.hid_path = ""

        # 1. Check shurectl detection
        try:
            res = self._run_shurectl(["--list"], timeout=3.0)
            out = res.stdout
            if "Shure" in out and "/dev/hidraw" in out:
                found_shure = True
                for line in out.splitlines():
                    if "/dev/hidraw" in line:
                        parts = [p.strip() for p in line.split("|")]
                        if len(parts) >= 1:
                            self.hid_path = parts[0]
                        if len(parts) >= 2:
                            self.device_name = parts[1]
                        if len(parts) >= 3:
                            self.serial_number = parts[2].replace("S/N:", "").strip()
        except Exception:
            pass

        # 2. Check HID node permissions
        if self.hid_path and os.path.exists(self.hid_path):
            self.has_hid_permission = os.access(self.hid_path, os.R_OK | os.W_OK)
        else:
            self.has_hid_permission = False

        # 3. Read full hardware state if permitted
        if found_shure and self.has_hid_permission:
            self.read_hardware_state()

        # 4. Detect PipeWire / WirePlumber nodes
        old_source_id = self.source_node_id
        self.source_node_id = None
        self.sink_node_id = None
        try:
            res = subprocess.run(["wpctl", "status"], capture_output=True, text=True, timeout=3)
            out = res.stdout
            
            in_sources = False
            in_sinks = False
            for line in out.splitlines():
                if "Sources:" in line:
                    in_sources = True
                    in_sinks = False
                    continue
                elif "Sinks:" in line:
                    in_sinks = True
                    in_sources = False
                    continue
                elif "Streams:" in line or "Sink endpoints:" in line or "Source endpoints:" in line or "Video" in line:
                    in_sources = False
                    in_sinks = False
                    continue
                
                if "Shure" in line:
                    m = re.search(r'(\d+)\.\s+Shure', line)
                    if m:
                        node_id = int(m.group(1))
                        if in_sources:
                            self.source_node_id = node_id
                            found_shure = True
                        elif in_sinks:
                            self.sink_node_id = node_id
        except Exception:
            pass

        self.is_connected = found_shure
        if self.source_node_id is not None:
            try:
                subprocess.run(["wpctl", "set-default", str(self.source_node_id)], capture_output=True, timeout=2)
            except Exception:
                pass
        self.refresh_wpctl_state()

        # If source node changed, restart pw-record meter so it attaches to the right mic
        if self.source_node_id != old_source_id and self._meter_proc:
            try:
                self._meter_proc.kill()
            except Exception:
                pass

        if self.on_state_changed and (was_connected != self.is_connected or found_shure):
            self.on_state_changed()

    def read_hardware_state(self):
        """Query full DSP state from the Shure hardware via shurectl --json."""
        try:
            res = self._run_shurectl(["--json"], timeout=4.0)
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                self.device_name = data.get("model", self.device_name)
                self.firmware_version = data.get("firmware_version", "")
                serial = data.get("factory_serial") or data.get("serial_number")
                if serial and serial != "Unknown":
                    self.serial_number = serial
                self.is_muted = bool(data.get("muted", self.is_muted))
                self.mode = data.get("mode", self.mode)
                self.gain_db = float(data.get("gain_db", self.gain_db))
                self.tone = int(data.get("tone", self.tone))
                self.denoiser = bool(data.get("denoiser", self.denoiser))
                self.popper_stopper = bool(data.get("popper_stopper", self.popper_stopper))
                self.hpf = data.get("hpf", self.hpf)
                self.gain_locked = bool(data.get("gain_locked", self.gain_locked))
                if data.get("mute_btn_disabled") is not None:
                    self.mute_btn_disabled = bool(data.get("mute_btn_disabled"))
                self.monitor_mix = int(data.get("monitor_mix", self.monitor_mix))
        except Exception:
            pass

    def start_hotplug_watcher(self):
        """Background thread that watches for USB hotplug or permission changes."""
        def watcher():
            while True:
                time.sleep(2.5)
                try:
                    # Quick check if hidraw changed or connection state changed
                    devs = [f for f in os.listdir("/dev") if f.startswith("hidraw")]
                    if not self.is_connected or not self.has_hid_permission or (self.hid_path and not os.path.exists(self.hid_path)):
                        self.detect_devices()
                except Exception:
                    pass

        self._hotplug_thread = threading.Thread(target=watcher, daemon=True)
        self._hotplug_thread.start()

    def refresh_wpctl_state(self):
        """Read mute and volume state from WirePlumber."""
        if self.source_node_id is not None:
            try:
                res = subprocess.run(
                    ["wpctl", "get-volume", str(self.source_node_id)],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                out = res.stdout.strip()
                if not self.has_hid_permission:
                    self.is_muted = "[MUTED]" in out
                m = re.search(r'Volume:\s+([0-9.]+)', out)
                if m:
                    self.volume = float(m.group(1))
            except Exception:
                pass

        if self.sink_node_id is not None:
            try:
                res = subprocess.run(
                    ["wpctl", "get-volume", str(self.sink_node_id)],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                m = re.search(r'Volume:\s+([0-9.]+)', res.stdout)
                if m:
                    self.monitor_volume = float(m.group(1))
            except Exception:
                pass

    def set_mute(self, muted: bool):
        """Set mute state both via PipeWire and shurectl."""
        self.is_muted = muted
        if self.source_node_id is not None:
            val = "1" if muted else "0"
            subprocess.run(["wpctl", "set-mute", str(self.source_node_id), val], capture_output=True)
        
        self._set_hw_async(f"mute={'on' if muted else 'off'}")

        if self.on_state_changed:
            self.on_state_changed()

    def toggle_mute(self):
        self.set_mute(not self.is_muted)

    def set_gain(self, gain_db: float):
        """Set gain in dB (0.0 to 36.0 for MV6)."""
        self.gain_db = max(0.0, min(36.0, gain_db))
        tenths = int(round(self.gain_db * 10))
        self._set_hw_async(f"gain_tenths={tenths}")

    def set_mode(self, mode: str):
        """Auto Level vs Manual."""
        self.mode = mode
        self._set_hw_async(f"mode={mode}")
        if self.on_state_changed:
            self.on_state_changed()

    def set_tone(self, tone: int):
        """Tone preset: -10 (Dark), 0 (Natural), +10 (Bright)."""
        self.tone = max(-10, min(10, int(tone)))
        self._set_hw_async(f"tone={self.tone}")

    def set_denoiser(self, enabled: bool):
        self.denoiser = enabled
        self._set_hw_async(f"denoiser={'true' if enabled else 'false'}")

    def set_popper_stopper(self, enabled: bool):
        self.popper_stopper = enabled
        self._set_hw_async(f"popper_stopper={'true' if enabled else 'false'}")

    def set_hpf(self, freq: str):
        self.hpf = freq
        self._set_hw_async(f"hpf={freq}")

    def set_gain_locked(self, locked: bool):
        self.gain_locked = locked
        self._set_hw_async(f"gain_lock={'true' if locked else 'false'}")

    def set_mute_btn_disabled(self, disabled: bool):
        self.mute_btn_disabled = disabled
        self._set_hw_async(f"mute_btn_disable={'true' if disabled else 'false'}")

    def load_preset(self, slot: int):
        """Load preset 1..4."""
        if self.has_hid_permission:
            def worker():
                try:
                    self._run_shurectl(["--preset", str(slot)], timeout=4.0)
                    self.read_hardware_state()
                    if self.on_state_changed:
                        self.on_state_changed()
                except Exception:
                    pass
            threading.Thread(target=worker, daemon=True).start()

    def start_meter(self):
        """Start real-time audio VU metering via PipeWire pw-record."""
        if self._meter_running:
            return
        self._meter_running = True
        self._meter_thread = threading.Thread(target=self._meter_worker, daemon=True)
        self._meter_thread.start()

    def _meter_worker(self):
        while self._meter_running:
            cmd = ["pw-record", "--format", "s16", "--rate", "16000", "--channels", "1"]
            if self.source_node_id is not None:
                cmd.extend(["--target", str(self.source_node_id)])
            cmd.append("-")

            try:
                self._meter_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )
                chunk_samples = 400  # 25ms chunks @ 16kHz
                bytes_per_chunk = chunk_samples * 2

                while self._meter_running and self._meter_proc.poll() is None:
                    raw = self._meter_proc.stdout.read(bytes_per_chunk)
                    if not raw or len(raw) < 2:
                        time.sleep(0.05)
                        continue

                    if self.is_muted:
                        self.current_db = -60.0
                        if time.time() - self._peak_decay_time > 1.5:
                            self.peak_db = -60.0
                        if self.on_meter_update:
                            self.on_meter_update(self.current_db, self.peak_db)
                        continue

                    n_samples = len(raw) // 2
                    samples = struct.unpack(f"<{n_samples}h", raw[:n_samples * 2])
                    peak_val = max(abs(s) for s in samples)

                    if peak_val > 0:
                        db = 20.0 * math.log10(peak_val / 32767.0)
                        db = max(-60.0, min(0.0, db))
                    else:
                        db = -60.0

                    self.current_db = db
                    if db > self.peak_db:
                        self.peak_db = db
                        self._peak_decay_time = time.time()
                    elif time.time() - self._peak_decay_time > 1.2:
                        self.peak_db = max(-60.0, self.peak_db - 1.5)

                    if self.on_meter_update:
                        self.on_meter_update(self.current_db, self.peak_db)

            except Exception:
                time.sleep(1.0)
            finally:
                if self._meter_proc:
                    try:
                        self._meter_proc.kill()
                    except Exception:
                        pass
                time.sleep(0.5)

    def stop_meter(self):
        self._meter_running = False
        if self._meter_proc:
            try:
                self._meter_proc.kill()
            except Exception:
                pass
