#!/usr/bin/env python3
"""
shurectl-gui — Native Desktop Audio Control for Shure USB Microphones
Built with GTK 4 & Libadwaita for GNOME / Linux
"""

import os
import sys

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk

from shure_controller import ShureController


METER_CSS = b"""
.vu-well {
    background-color: #111318;
    border-radius: 10px;
    padding: 10px 12px;
    border: 1px solid rgba(255, 255, 255, 0.08);
}
.vu-seg {
    min-width: 6px;
    min-height: 24px;
    border-radius: 3px;
}
.vu-green-off {
    background-color: rgba(46, 204, 113, 0.16);
}
.vu-green-on {
    background-color: #2ecc71;
    box-shadow: 0 0 6px rgba(46, 204, 113, 0.55);
}
.vu-amber-off {
    background-color: rgba(241, 196, 15, 0.16);
}
.vu-amber-on {
    background-color: #f1c40f;
    box-shadow: 0 0 6px rgba(241, 196, 15, 0.55);
}
.vu-red-off {
    background-color: rgba(231, 76, 60, 0.16);
}
.vu-red-on {
    background-color: #ff4757;
    box-shadow: 0 0 8px rgba(255, 71, 87, 0.75);
}
.vu-peak {
    background-color: #ffffff;
    box-shadow: 0 0 6px rgba(255, 255, 255, 0.9);
}
"""


class VUMeterWidget(Gtk.Box):
    """Studio-grade 36-segment hardware-accelerated GTK4 LED VU Level Meter."""
    NUM_SEGMENTS = 36

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        self.set_hexpand(True)
        self.add_css_class("vu-well")

        self.segments = []
        self.seg_colors = []  # "green", "amber", or "red"

        for i in range(self.NUM_SEGMENTS):
            seg = Gtk.Box()
            seg.set_hexpand(True)
            seg.add_css_class("vu-seg")

            norm = (i + 1) / self.NUM_SEGMENTS
            if norm <= 0.70:
                color = "green"
            elif norm <= 0.92:
                color = "amber"
            else:
                color = "red"

            seg.add_css_class(f"vu-{color}-off")
            self.segments.append(seg)
            self.seg_colors.append(color)
            self.append(seg)

        self._last_active = -1
        self._last_peak = -1

    def update_levels(self, current_db: float, peak_db: float):
        def db_to_idx(db: float) -> int:
            if db <= -60.0:
                return 0
            if db >= 0.0:
                return self.NUM_SEGMENTS
            norm = (db + 60.0) / 60.0
            return int(round(norm * self.NUM_SEGMENTS))

        active_count = db_to_idx(current_db)
        peak_idx = db_to_idx(peak_db) - 1

        if active_count == self._last_active and peak_idx == self._last_peak:
            return

        self._last_active = active_count
        self._last_peak = peak_idx

        for i, seg in enumerate(self.segments):
            color = self.seg_colors[i]
            on_cls = f"vu-{color}-on"
            off_cls = f"vu-{color}-off"

            if i == peak_idx and peak_idx >= active_count and peak_idx >= 1:
                seg.remove_css_class(on_cls)
                seg.remove_css_class(off_cls)
                seg.add_css_class("vu-peak")
            elif i < active_count:
                seg.remove_css_class("vu-peak")
                seg.remove_css_class(off_cls)
                seg.add_css_class(on_cls)
            else:
                seg.remove_css_class("vu-peak")
                seg.remove_css_class(on_cls)
                seg.add_css_class(off_cls)


class ShureMainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Shure Audio Control")
        self.set_default_size(540, 740)
        self._syncing_ui = False

        # Load custom CSS for VU meter
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(METER_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self._build_ui()

        self.controller = ShureController()
        self.controller.on_state_changed = self.on_device_state_changed
        self.controller.on_meter_update = self.on_meter_update_callback

        self.refresh_ui()

    def _build_ui(self):
        # Main vertical box
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)

        # Header Bar
        header = Adw.HeaderBar()
        main_box.append(header)

        # Title widget
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_lbl = Gtk.Label(label="Shure Audio Control")
        title_lbl.add_css_class("heading")
        self.subtitle_lbl = Gtk.Label(label="Detecting device...")
        self.subtitle_lbl.add_css_class("caption")
        self.subtitle_lbl.add_css_class("dim-label")
        title_box.append(title_lbl)
        title_box.append(self.subtitle_lbl)
        header.set_title_widget(title_box)

        # Header Quick Mute Button
        self.header_mute_btn = Gtk.Button()
        self.header_mute_btn.set_icon_name("audio-input-microphone-symbolic")
        self.header_mute_btn.set_tooltip_text("Toggle Microphone Mute")
        self.header_mute_btn.connect("clicked", lambda b: self.controller.toggle_mute())
        header.pack_end(self.header_mute_btn)

        # Refresh button
        refresh_btn = Gtk.Button()
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Rescan Devices & Reload Hardware State")
        refresh_btn.connect("clicked", lambda b: self.on_rescan_clicked())
        header.pack_end(refresh_btn)

        # Banner for Udev Permission Warning
        self.banner = Adw.Banner(
            title="Hardware DSP controls require udev permissions. Audio monitor is active.",
            button_label="Copy Setup Command",
            revealed=False
        )
        self.banner.connect("button-clicked", self.on_copy_udev_clicked)
        main_box.append(self.banner)

        # Scrolled content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        main_box.append(scrolled)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(520)
        clamp.set_tightening_threshold(480)
        scrolled.set_child(clamp)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.set_margin_top(14)
        page.set_margin_bottom(24)
        page.set_margin_start(16)
        page.set_margin_end(16)
        clamp.set_child(page)

        # ── 1. Live Meter Card ──
        meter_group = Adw.PreferencesGroup()
        meter_group.set_title("Input Level Meter")
        
        meter_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        meter_card.add_css_class("card")
        meter_card.set_margin_top(4)
        meter_card.set_margin_bottom(4)
        meter_card.set_margin_start(4)
        meter_card.set_margin_end(4)

        self.vu_meter = VUMeterWidget()
        self.vu_meter.set_margin_top(10)
        self.vu_meter.set_margin_start(10)
        self.vu_meter.set_margin_end(10)
        meter_card.append(self.vu_meter)

        meter_labels = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        meter_labels.set_margin_start(12)
        meter_labels.set_margin_end(12)
        meter_labels.set_margin_bottom(8)
        
        scale_lbl = Gtk.Label(label="-60 dB         -36 dB         -18 dB     -6 dB  0 dB")
        scale_lbl.add_css_class("caption")
        scale_lbl.add_css_class("dim-label")
        scale_lbl.set_hexpand(True)
        scale_lbl.set_halign(Gtk.Align.START)
        
        self.db_readout = Gtk.Label(label="-60.0 dBFS  (Peak: -60.0)")
        self.db_readout.add_css_class("caption")
        self.db_readout.add_css_class("numeric")
        self.db_readout.set_halign(Gtk.Align.END)
        
        meter_labels.append(scale_lbl)
        meter_labels.append(self.db_readout)
        meter_card.append(meter_labels)
        meter_group.add(meter_card)
        page.append(meter_group)

        # ── 2. Microphone & Gain Control ──
        mic_group = Adw.PreferencesGroup()
        mic_group.set_title("Microphone &amp; Gain")
        page.append(mic_group)

        # Big Mute Switch Row
        self.mute_row = Adw.ActionRow(title="Mute Microphone", subtitle="Mutes input to all apps")
        self.mute_switch = Gtk.Switch()
        self.mute_switch.set_valign(Gtk.Align.CENTER)
        self.mute_switch.connect("notify::active", self.on_mute_switch_toggled)
        self.mute_row.add_suffix(self.mute_switch)
        mic_group.add(self.mute_row)

        # Mode Selection (Auto Level vs Manual)
        self.mode_row = Adw.ActionRow(title="Mode", subtitle="Smart dynamic gain vs fixed manual level")
        self.mode_btn_auto = Gtk.ToggleButton(label="Auto Level")
        self.mode_btn_manual = Gtk.ToggleButton(label="Manual")
        self.mode_btn_manual.set_group(self.mode_btn_auto)
        self.mode_btn_manual.set_active(True)
        self.mode_btn_auto.connect("toggled", self.on_mode_toggled)
        
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        mode_box.add_css_class("linked")
        mode_box.set_valign(Gtk.Align.CENTER)
        mode_box.append(self.mode_btn_auto)
        mode_box.append(self.mode_btn_manual)
        self.mode_row.add_suffix(mode_box)
        mic_group.add(self.mode_row)

        # Manual Gain Slider
        self.gain_row = Adw.ActionRow(title="Preamp Gain", subtitle="+18.0 dB")
        gain_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        gain_box.set_valign(Gtk.Align.CENTER)
        
        self.gain_adj = Gtk.Adjustment(value=18.0, lower=0.0, upper=36.0, step_increment=0.5, page_increment=3.0)
        self.gain_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.gain_adj)
        self.gain_scale.set_size_request(160, -1)
        self.gain_scale.set_digits(1)
        self.gain_scale.set_draw_value(False)
        self.gain_adj.connect("value-changed", self.on_gain_changed)
        gain_box.append(self.gain_scale)
        
        self.gain_val_lbl = Gtk.Label(label="+18.0 dB")
        self.gain_val_lbl.set_size_request(60, -1)
        self.gain_val_lbl.add_css_class("numeric")
        gain_box.append(self.gain_val_lbl)
        
        self.gain_row.add_suffix(gain_box)
        mic_group.add(self.gain_row)

        # Quick Gain Preset Buttons
        self.gain_presets_row = Adw.ActionRow(title="Quick Gain Presets")
        presets_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        presets_box.set_valign(Gtk.Align.CENTER)
        for val in [0.0, 12.0, 24.0, 36.0]:
            btn = Gtk.Button(label=f"+{int(val)} dB")
            btn.add_css_class("flat")
            btn.connect("clicked", lambda b, v=val: self.gain_adj.set_value(v))
            presets_box.append(btn)
        self.gain_presets_row.add_suffix(presets_box)
        mic_group.add(self.gain_presets_row)

        # ── 3. DSP & Audio Enhancement ──
        dsp_group = Adw.PreferencesGroup()
        dsp_group.set_title("Audio DSP &amp; Acoustics")
        page.append(dsp_group)

        # Tone Character (Dark, Natural, Bright)
        tone_row = Adw.ActionRow(title="Voice Tone", subtitle="Microphone frequency response profile")
        self.tone_dark = Gtk.ToggleButton(label="Dark")
        self.tone_nat = Gtk.ToggleButton(label="Natural")
        self.tone_bright = Gtk.ToggleButton(label="Bright")
        self.tone_nat.set_group(self.tone_dark)
        self.tone_bright.set_group(self.tone_dark)
        self.tone_nat.set_active(True)
        
        self.tone_dark.connect("toggled", self.on_tone_toggled, -10)
        self.tone_nat.connect("toggled", self.on_tone_toggled, 0)
        self.tone_bright.connect("toggled", self.on_tone_toggled, 10)

        tone_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        tone_box.add_css_class("linked")
        tone_box.set_valign(Gtk.Align.CENTER)
        tone_box.append(self.tone_dark)
        tone_box.append(self.tone_nat)
        tone_box.append(self.tone_bright)
        tone_row.add_suffix(tone_box)
        dsp_group.add(tone_row)

        # Real-time Denoiser
        self.denoiser_row = Adw.SwitchRow(title="Real-Time Denoiser", subtitle="Removes air conditioning and ambient room noise")
        self.denoiser_row.set_active(True)
        self.denoiser_row.connect("notify::active", self.on_denoiser_toggled)
        dsp_group.add(self.denoiser_row)

        # Digital Popper Stopper
        self.popper_row = Adw.SwitchRow(title="Digital Popper Stopper", subtitle="Eliminates harsh plosives and breath pops")
        self.popper_row.set_active(True)
        self.popper_row.connect("notify::active", self.on_popper_toggled)
        dsp_group.add(self.popper_row)

        # High-Pass Filter (HPF)
        hpf_row = Adw.ActionRow(title="High-Pass Filter (HPF)", subtitle="Cuts low-frequency desk rumble")
        self.hpf_off = Gtk.ToggleButton(label="Off")
        self.hpf_75 = Gtk.ToggleButton(label="75 Hz")
        self.hpf_150 = Gtk.ToggleButton(label="150 Hz")
        self.hpf_75.set_group(self.hpf_off)
        self.hpf_150.set_group(self.hpf_off)
        self.hpf_off.set_active(True)

        self.hpf_off.connect("toggled", self.on_hpf_toggled, "off")
        self.hpf_75.connect("toggled", self.on_hpf_toggled, "75hz")
        self.hpf_150.connect("toggled", self.on_hpf_toggled, "150hz")

        hpf_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        hpf_box.add_css_class("linked")
        hpf_box.set_valign(Gtk.Align.CENTER)
        hpf_box.append(self.hpf_off)
        hpf_box.append(self.hpf_75)
        hpf_box.append(self.hpf_150)
        hpf_row.add_suffix(hpf_box)
        dsp_group.add(hpf_row)

        # ── 4. Hardware Protections ──
        prot_group = Adw.PreferencesGroup()
        prot_group.set_title("Hardware Safeguards")
        page.append(prot_group)

        self.lock_row = Adw.SwitchRow(title="Gain Lock", subtitle="Prevent accidental changes to manual gain")
        self.lock_row.connect("notify::active", self.on_gain_lock_toggled)
        prot_group.add(self.lock_row)

        # ── 5. Presets ──
        preset_group = Adw.PreferencesGroup()
        preset_group.set_title("Presets")
        page.append(preset_group)

        preset_row = Adw.ActionRow(title="Hardware Presets", subtitle="Restore complete saved device configurations")
        presets_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        presets_btn_box.set_valign(Gtk.Align.CENTER)
        
        for slot in [1, 2, 3, 4]:
            btn = Gtk.Button(label=f"Slot {slot}")
            btn.connect("clicked", lambda b, s=slot: self.on_load_preset_clicked(s))
            presets_btn_box.append(btn)
            
        preset_row.add_suffix(presets_btn_box)
        preset_group.add(preset_row)

    def refresh_ui(self):
        """Update widgets to match controller hardware state without triggering feedback writes."""
        self._syncing_ui = True
        try:
            if self.controller.is_connected:
                serial = f" • S/N: {self.controller.serial_number}" if self.controller.serial_number else ""
                fw = f" • FW {self.controller.firmware_version}" if self.controller.firmware_version else ""
                self.subtitle_lbl.set_label(f"Connected: {self.controller.device_name}{serial}{fw}")
                self.banner.set_revealed(not self.controller.has_hid_permission)
            else:
                self.subtitle_lbl.set_label("No device found — Plug in your Shure microphone")
                self.banner.set_revealed(False)

            # Mute state
            is_muted = self.controller.is_muted
            if self.mute_switch.get_active() != is_muted:
                self.mute_switch.set_active(is_muted)
            
            if is_muted:
                self.header_mute_btn.add_css_class("destructive-action")
                self.header_mute_btn.remove_css_class("suggested-action")
                self.header_mute_btn.set_icon_name("microphone-sensitivity-muted-symbolic")
                self.mute_row.set_subtitle("MICROPHONE MUTED (No audio captured)")
            else:
                self.header_mute_btn.remove_css_class("destructive-action")
                self.header_mute_btn.set_icon_name("audio-input-microphone-symbolic")
                self.mute_row.set_subtitle("Microphone active &amp; live")

            # Mode
            is_auto = (self.controller.mode == "auto")
            self.mode_btn_auto.set_active(is_auto)
            self.mode_btn_manual.set_active(not is_auto)
            can_adjust_gain = (not is_auto) and (not self.controller.gain_locked)
            self.gain_row.set_sensitive(can_adjust_gain)
            self.gain_presets_row.set_sensitive(can_adjust_gain)

            # Gain
            self.gain_adj.set_value(self.controller.gain_db)
            self.gain_val_lbl.set_label(f"+{self.controller.gain_db:.1f} dB")
            self.gain_row.set_subtitle(
                "Managed automatically by Auto Level" if is_auto else f"+{self.controller.gain_db:.1f} dB"
            )

            # Tone
            if self.controller.tone <= -5:
                self.tone_dark.set_active(True)
            elif self.controller.tone >= 5:
                self.tone_bright.set_active(True)
            else:
                self.tone_nat.set_active(True)

            # Denoiser & Popper Stopper
            self.denoiser_row.set_active(self.controller.denoiser)
            self.popper_row.set_active(self.controller.popper_stopper)

            # HPF
            if self.controller.hpf == "75hz":
                self.hpf_75.set_active(True)
            elif self.controller.hpf == "150hz":
                self.hpf_150.set_active(True)
            else:
                self.hpf_off.set_active(True)

            # Gain Lock
            self.lock_row.set_active(self.controller.gain_locked)
        finally:
            self._syncing_ui = False

    def on_mute_switch_toggled(self, switch, gparam):
        if self._syncing_ui:
            return
        self.controller.set_mute(switch.get_active())

    def on_mode_toggled(self, btn):
        if self._syncing_ui:
            return
        if btn.get_active():
            self.controller.set_mode("auto")
        else:
            self.controller.set_mode("manual")
        self.refresh_ui()

    def on_gain_changed(self, adj):
        val = adj.get_value()
        self.gain_val_lbl.set_label(f"+{val:.1f} dB")
        if self._syncing_ui:
            return
        self.gain_row.set_subtitle(f"+{val:.1f} dB")
        self.controller.set_gain(val)

    def on_tone_toggled(self, btn, tone_val):
        if self._syncing_ui or not btn.get_active():
            return
        self.controller.set_tone(tone_val)

    def on_denoiser_toggled(self, row, gparam):
        if self._syncing_ui:
            return
        self.controller.set_denoiser(row.get_active())

    def on_popper_toggled(self, row, gparam):
        if self._syncing_ui:
            return
        self.controller.set_popper_stopper(row.get_active())

    def on_hpf_toggled(self, btn, freq_str):
        if self._syncing_ui or not btn.get_active():
            return
        self.controller.set_hpf(freq_str)

    def on_gain_lock_toggled(self, row, gparam):
        if self._syncing_ui:
            return
        self.controller.set_gain_locked(row.get_active())
        self.refresh_ui()

    def on_load_preset_clicked(self, slot: int):
        self.controller.load_preset(slot)

    def on_rescan_clicked(self):
        self.controller.detect_devices()
        self.refresh_ui()

    def on_copy_udev_clicked(self, banner):
        cmd = "sudo cp /home/paparoot/.gemini/antigravity/scratch/shurectl/62-shure.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger"
        clipboard = self.get_display().get_clipboard()
        clipboard.set(cmd)
        banner.set_title("Command copied! Paste in terminal with sudo to activate.")

    def on_device_state_changed(self):
        GLib.idle_add(self.refresh_ui)

    def on_meter_update_callback(self, current_db: float, peak_db: float):
        def update():
            self.vu_meter.update_levels(current_db, peak_db)
            if self.controller.is_muted:
                self.db_readout.set_label("MUTED")
            else:
                self.db_readout.set_label(f"{current_db:.1f} dBFS  (Peak: {peak_db:.1f})")
            return False
        GLib.idle_add(update)


class ShureApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.shure.shurectl.gui",
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = ShureMainWindow(application=self)
        win.present()


def main():
    app = ShureApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
