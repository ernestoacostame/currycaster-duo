#!/usr/bin/env python3
print("DEBUG: Starting Broadcast System (v41.5 - Optimized for RØDECaster Duo)...")
import sys, subprocess, struct, json, os, time, gi, mido, pulsectl
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QFileDialog, QLabel, QFrame, QSlider, QTreeView, QMenu,
                             QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
                             QTabWidget, QGridLayout, QInputDialog, QColorDialog, QMainWindow,
                             QFontDialog)
from PyQt6.QtCore import (QTimer, Qt, pyqtSignal, QDir, QThread, QObject, QSortFilterProxyModel, QMimeData, QUrl, QTime)
from PyQt6.QtGui import (QPainter, QColor, QPen, QAction, QFileSystemModel, QShortcut, QKeySequence, QDrag, QFont)

gi.require_version('Gst', '1.0')
gi.require_version('GstPbutils', '1.0')
from gi.repository import Gst, GObject, GstPbutils

# --- CONFIGURATION PATHS ---
USER_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "currycaster")
os.makedirs(USER_CONFIG_DIR, exist_ok=True)
MIDI_CONFIG_FILE = os.path.join(USER_CONFIG_DIR, "midi_config.json")
AUDIO_CONFIG_FILE = os.path.join(USER_CONFIG_DIR, "audio_config.json")
LIBRARY_INDEX_FILE = os.path.join(USER_CONFIG_DIR, "library_index.json")
CART_CONFIG_FILE = os.path.join(USER_CONFIG_DIR, "cart_config.json")
WINDOW_LAYOUT_FILE = os.path.join(USER_CONFIG_DIR, "window_layout.json")
EXPLORER_CONFIG_FILE = os.path.join(USER_CONFIG_DIR, "explorer_config.json")
VALID_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.flac', '.ogg', '.aac', '.wma'}

# --- GLOBAL HELPERS ---
def get_filepath_from_drop(e):
    if e.mimeData().hasUrls():
        for url in e.mimeData().urls():
            if url.isLocalFile(): return url.toLocalFile()
    if e.mimeData().hasText():
        return e.mimeData().text().replace("file://", "").strip()
    return None

def get_log_volume(linear_value):
    return (linear_value / 100.0) ** 3

# --- Window State Manager ---
class WindowStateManager:
    def __init__(self):
        self.data = {}
        self.load()
    def load(self):
        if os.path.exists(WINDOW_LAYOUT_FILE):
            try:
                with open(WINDOW_LAYOUT_FILE, 'r') as f: self.data = json.load(f)
            except: pass
    def save(self):
        try:
            with open(WINDOW_LAYOUT_FILE, 'w') as f: json.dump(self.data, f, indent=4)
        except: pass
    def apply(self, widget, name):
        if name in self.data:
            g = self.data[name]
            widget.move(g['x'], g['y']); widget.resize(g['w'], g['h'])
    def record(self, widget, name):
        self.data[name] = {'x': widget.x(), 'y': widget.y(), 'w': widget.width(), 'h': widget.height()}
        self.save()

window_manager = WindowStateManager()

# --- 1. SEARCH INDEXER WORKER ---
class FileIndexerWorker(QThread):
    index_finished = pyqtSignal(list)
    def __init__(self, root_path):
        super().__init__(); self.root_path = root_path
    def run(self):
        db = []
        try:
            for root, dirs, files in os.walk(self.root_path):
                if any(x in root for x in ["/.", "/found.", "$RECYCLE"]): continue
                for file in files:
                    if file.startswith('.') or os.path.splitext(file)[1].lower() not in VALID_EXTENSIONS: continue
                    try:
                        fp = os.path.join(root, file)
                        db.append((file, fp, os.path.getmtime(fp)))
                    except: pass
        except: pass
        db.sort(key=lambda x: x[2], reverse=True)
        self.index_finished.emit(db)

# --- 2. AUDIO ROUTER (ADAPTADO PARA RØDE DUO) ---
class AudioRouter:
    def __init__(self):
        self.pulse = pulsectl.Pulse('currycaster-router')
        self.active_players, self.active_cart_ids = [], []
        self.refresh_devices()

        # Mapeo automático a los nombres definidos en nuestro WirePlumber conf
        self.global_pgm_sink = self.find_sink_by_name("USB 1 Main-In")
        self.global_cue_sink = self.find_sink_by_name("USB 1 Chat-Out")

        # Fallback si los nombres personalizados no se encuentran
        if not self.global_pgm_sink and self.sinks: self.global_pgm_sink = self.sinks[0].name
        if not self.global_cue_sink and self.sinks: self.global_cue_sink = self.sinks[0].name

        self.pending_routes = {}
        self.load_config()

    def find_sink_by_name(self, friendly_name):
        """Busca el dispositivo por la descripción que pusimos en el PKGBUILD"""
        for s in self.sinks:
            if friendly_name in s.description or friendly_name in s.name:
                return s.name
        return None

    def refresh_devices(self):
        self.sinks = self.pulse.sink_list()
        return self.sinks

    def load_config(self):
        if not os.path.exists(AUDIO_CONFIG_FILE): return
        try:
            with open(AUDIO_CONFIG_FILE, 'r') as f:
                d = json.load(f)
                an = [s.name for s in self.sinks]
                if d.get("program_sink") in an: self.global_pgm_sink = d["program_sink"]
                if d.get("cue_sink") in an: self.global_cue_sink = d["cue_sink"]
        except: pass

    def save_config(self):
        try:
            with open(AUDIO_CONFIG_FILE, 'w') as f:
                json.dump({"program_sink": self.global_pgm_sink, "cue_sink": self.global_cue_sink}, f, indent=4)
        except: pass

    def set_program_device(self, name): self.global_pgm_sink = name; self.save_config(); self.move_all_streams()
    def set_cue_device(self, name): self.global_cue_sink = name; self.save_config(); self.move_all_streams()

    def register_player(self, p):
        if p not in self.active_players: self.active_players.append(p)
    def register_cart_id(self, cid):
        if cid not in self.active_cart_ids: self.active_cart_ids.append(cid)

    def move_all_streams(self):
        for p in self.active_players: self.route_stream(f"Currycaster_Player_{p.player_id}", p.pfl)
        for cid in self.active_cart_ids: self.route_stream(cid, False)

    def route_stream(self, app_id, is_cue, retries=8):
        if app_id in self.pending_routes: self.pending_routes[app_id] = False
        target = self.global_cue_sink if is_cue else self.global_pgm_sink
        self.pending_routes[app_id] = True
        def attempt(rem):
            if not self.pending_routes.get(app_id, False): return
            try:
                t_snk = next((s for s in self.pulse.sink_list() if s.name == target), None)
                t_str = next((i for i in self.pulse.sink_input_list() if i.proplist.get('application.name') == app_id), None)
                if t_str and t_snk:
                    if t_str.sink != t_snk.index:
                        self.pulse.sink_input_move(t_str.index, t_snk.index)
                    self.pending_routes[app_id] = False
                elif rem > 0: QTimer.singleShot(150, lambda: attempt(rem - 1))
            except: pass
        attempt(retries)

    def route_player(self, pid, is_cue): self.route_stream(f"Currycaster_Player_{pid}", is_cue)

# --- 3. MIDI ENGINE (ADAPTADO PARA RØDE DUO) ---
class MidiWorker(QThread):
    midi_signal = pyqtSignal(str, int, int, int)
    def __init__(self):
        super().__init__(); self.running = True; self.port_name = None
    def run(self):
        try:
            av = mido.get_input_names()
            # Buscamos específicamente el puerto de la Duo
            self.port_name = next((n for n in av if any(x in n.lower() for x in ["rodecaster", "duo"])),
                                  av[0] if av else None)
            if not self.port_name:
                print("DEBUG: MIDI Device not found.")
                return
            with mido.open_input(self.port_name) as port:
                print(f"DEBUG: MIDI Connected to {self.port_name}")
                while self.running:
                    for m in port.iter_pending():
                        if m.type == 'control_change': self.midi_signal.emit('cc', m.channel, m.control, m.value)
                        elif m.type == 'note_on': self.midi_signal.emit('note', m.channel, m.note, m.velocity)
                    self.msleep(5)
        except: pass
    def stop(self): self.running = False; self.wait()

class MidiMapper(QObject):
    def __init__(self):
        super().__init__(); self.l_map, self.r_map, self.registry = {}, {}, {}
        self.learning_uid = None; self.load()
    def load(self):
        if os.path.exists(MIDI_CONFIG_FILE):
            try:
                with open(MIDI_CONFIG_FILE, 'r') as f: self.l_map = json.load(f)
            except: pass
    def save(self):
        try:
            with open(MIDI_CONFIG_FILE, 'w') as f: json.dump(self.l_map, f, indent=4)
        except: pass
    def register(self, uid, cb):
        self.registry[uid] = cb
        if uid in self.l_map: self.r_map[self.l_map[uid]] = cb
    def handle(self, mt, ch, idx, val):
        uk = f"{mt}:{ch}:{idx}"
        if self.learning_uid and val > 0:
            self.l_map[self.learning_uid] = uk; self.save()
            self.r_map[uk] = self.registry[self.learning_uid]; self.learning_uid = None
            return
        if uk in self.r_map:
            try: self.r_map[uk](val)
            except: pass
    def start_learning(self, uid): self.learning_uid = uid

# --- 4. UI COMPONENTS ---
class MidiButton(QPushButton):
    def __init__(self, t, uid, mm, cb): super().__init__(t); self.uid = uid; self.mm = mm; mm.register(uid, lambda v: cb() if v > 0 else None)
    def contextMenuEvent(self, e):
        m = QMenu(self); m.addAction(f"MIDI Learn ({self.uid})", lambda: self.mm.start_learning(self.uid)); m.exec(e.globalPos())

class MidiSlider(QSlider):
    def __init__(self, o, uid, mm, cb): super().__init__(o); self.uid = uid; self.mm = mm; mm.register(uid, lambda v: cb(int((v/127.0)*100)))
    def contextMenuEvent(self, e):
        m = QMenu(self); m.addAction(f"MIDI Learn ({self.uid})", lambda: self.mm.start_learning(self.uid)); m.exec(e.globalPos())

class ClickableLabel(QLabel):
    clicked = pyqtSignal(); right_clicked = pyqtSignal()
    def mousePressEvent(self, e): (e.button()==Qt.MouseButton.LeftButton and self.clicked.emit()) or (e.button()==Qt.MouseButton.RightButton and self.right_clicked.emit())

class DraggableListWidget(QListWidget):
    def startDrag(self, a):
        i = self.currentItem()
        if i:
            m = QMimeData(); m.setText(i.data(Qt.ItemDataRole.UserRole)); m.setUrls([QUrl.fromLocalFile(i.data(Qt.ItemDataRole.UserRole))])
            d = QDrag(self); d.setMimeData(m); d.exec(Qt.DropAction.CopyAction)

# --- 5. WAVEFORM WIDGET ---
class WaveformWidget(QFrame):
    seek_requested = pyqtSignal(float)
    cue_points_changed = pyqtSignal(float, float)
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(80)
        self.setStyleSheet("background-color: #222; border: 1px solid #444;")
        self.waveform_data, self.position_percent = [], 0.0
        self.cue_in, self.cue_out = 0.0, 1.0
        self.zoom_level, self.view_offset = 1.0, 0.0
        self.last_mouse_x, self.is_panning, self.has_moved = 0, False, False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
    def load_waveform_from_file(self, file_path):
        self.waveform_data, self.zoom_level, self.view_offset = [], 1.0, 0.0
        self.cue_in, self.cue_out = 0.0, 1.0
        self.cue_points_changed.emit(0.0, 1.0)
        cmd = ['ffmpeg', '-i', file_path, '-f', 's16le', '-ac', '1', '-acodec', 'pcm_s16le', '-ar', '200', '-vn', '-']
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if result.stdout:
                total_samples = len(result.stdout) // 2
                fmt = f"<{total_samples}h"
                samples = struct.unpack(fmt, result.stdout)
                abs_values = [abs(s) for s in samples]
                max_val = max(abs_values) if abs_values else 0
                self.waveform_data = [s / max_val if max_val > 0 else 0 for s in abs_values]
                self.update()
        except: pass
    def clear_waveform(self): self.waveform_data, self.position_percent = [], 0.0; self.update()
    def set_position(self, percent): self.position_percent = percent; self.update()
    def set_start_point(self, percent): self.cue_in = max(0.0, min(percent, self.cue_out)); self.cue_points_changed.emit(self.cue_in, self.cue_out); self.update()
    def set_end_point(self, percent): self.cue_out = min(1.0, max(percent, self.cue_in)); self.cue_points_changed.emit(self.cue_in, self.cue_out); self.update()
    def reset_clip(self): self.cue_in, self.cue_out = 0.0, 1.0; self.cue_points_changed.emit(0.0, 1.0); self.update()
    def wheelEvent(self, event):
        if not self.waveform_data: return
        delta = event.angleDelta().y(); zoom_factor = 1.1 if delta > 0 else 0.9
        new_zoom = max(1.0, min(50.0, self.zoom_level * zoom_factor))
        mx, w = event.position().x(), self.width()
        tp_old = w * self.zoom_level
        mouse_pct = (mx + self.view_offset) / tp_old if tp_old > 0 else 0
        self.zoom_level = new_zoom
        self.view_offset = max(0, min((mouse_pct * w * self.zoom_level) - mx, (w * self.zoom_level) - w))
        self.update()
    def mousePressEvent(self, event):
        self.last_mouse_x, self.has_moved = event.position().x(), False
        if event.button() == Qt.MouseButton.LeftButton:
            self.seek_requested.emit(max(0.0, min(1.0, (event.position().x() + self.view_offset) / (self.width() * self.zoom_level))))
        elif event.button() == Qt.MouseButton.RightButton:
            self.is_panning = True; self.setCursor(Qt.CursorShape.ClosedHandCursor)
    def mouseMoveEvent(self, event):
        if self.is_panning:
            dx = event.position().x() - self.last_mouse_x
            if abs(dx) > 2: self.has_moved = True
            self.last_mouse_x = event.position().x()
            self.view_offset = max(0, min(self.view_offset - dx, (self.width() * self.zoom_level) - self.width()))
            self.update()
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.is_panning = False; self.setCursor(Qt.CursorShape.PointingHandCursor)
            if not self.has_moved:
                pct = max(0.0, min(1.0, (event.position().x() + self.view_offset) / (self.width() * self.zoom_level)))
                self.show_context_menu(event.globalPosition().toPoint(), pct)
    def show_context_menu(self, global_pos, percent):
        m = QMenu(self)
        m.addAction("Set Start (Cue In)", lambda: self.set_start_point(percent))
        m.addAction("Set End (Cue Out)", lambda: self.set_end_point(percent))
        m.addSeparator(); m.addAction("Reset Clip", self.reset_clip); m.exec(global_pos)
    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        mid_h = h / 2
        if not self.waveform_data: return
        p_br, p_dm = QPen(QColor("#00bcd4")), QPen(QColor("#444444"))
        num_samples = len(self.waveform_data)
        virtual_width = w * self.zoom_level
        step = virtual_width / num_samples
        for i in range(max(0, int(self.view_offset / step)), min(num_samples, int((self.view_offset + w) / step) + 1)):
            x = (i * step) - self.view_offset
            amp = self.waveform_data[i] * mid_h * 0.95
            painter.setPen(p_br if self.cue_in <= i/num_samples <= self.cue_out else p_dm)
            painter.drawLine(int(x), int(mid_h - amp), int(x), int(mid_h + amp))
        for color, pct in [("#00ff00", self.cue_in), ("#ff0000", self.cue_out), ("#ffffff", self.position_percent)]:
            x = (pct * virtual_width) - self.view_offset
            if 0 <= x <= w:
                painter.setPen(QPen(QColor(color), 2)); painter.drawLine(int(x), 0, int(x), h)

# --- 6. PLAYER MODULE ---
class PlayerModule(QWidget):
    def __init__(self, pid, mm, ar):
        super().__init__(); self.setAcceptDrops(True)
        self.player_id, self.mm, self.ar = pid, mm, ar
        self.pfl, self.playing, self.dur = False, False, 0
        self.c_in, self.c_out, self.seek_req = 0.0, 1.0, True
        self.show_remaining = True
        self.ar.register_player(self); Gst.init(None); self.pipeline = None
        self.setStyleSheet("border-right: 1px solid #333; background: #1a1a1a;")
        self.setFixedWidth(240)
        lay = QHBoxLayout(self); lay.setContentsMargins(2,2,2,2)
        l_cnt = QWidget(); l_lay = QVBoxLayout(l_cnt); l_lay.setContentsMargins(0,0,0,0)
        self.vol = MidiSlider(Qt.Orientation.Vertical, f"p{pid}_vol", mm, self.set_v_internal)
        self.vol.setRange(0, 100); self.vol.setValue(100); self.vol.valueChanged.connect(self.set_v_engine)
        self.lbl_t = ClickableLabel("00:00"); self.lbl_t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_t.setStyleSheet("font-size:24px; font-weight:bold; color:#ff5555; font-family:monospace;")
        self.lbl_t.clicked.connect(self.toggle_time_mode)
        self.lbl_s = ClickableLabel(f"Player {pid}"); self.lbl_s.setWordWrap(True)
        self.lbl_s.setStyleSheet("color:#eee; font-size:12px; font-weight:bold; min-height:40px;")
        self.lbl_s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_s.clicked.connect(self.toggle_pfl); self.lbl_s.right_clicked.connect(self.open_menu)
        self.wf = WaveformWidget(); self.wf.seek_requested.connect(self.seek_audio); self.wf.cue_points_changed.connect(self.upd_clip)
        ctrls = QHBoxLayout()
        self.b_load = QPushButton("LOAD"); self.b_play = MidiButton("PLAY", f"p{pid}_play", mm, self.toggle_play)
        self.b_stop = MidiButton("STOP", f"p{pid}_stop", mm, self.stop_audio); self.b_dump = MidiButton("DUMP", f"p{pid}_dump", mm, self.dump_track)
        for b in [self.b_load, self.b_play, self.b_stop, self.b_dump]: b.setStyleSheet("background:#333; color:white; font-size:10px; padding:6px;")
        self.b_load.clicked.connect(self.load_dialog); self.b_play.clicked.connect(self.toggle_play)
        self.b_stop.clicked.connect(self.stop_audio); self.b_dump.clicked.connect(self.dump_track)
        l_lay.addWidget(self.lbl_t); l_lay.addWidget(self.lbl_s); l_lay.addWidget(self.wf)
        ctrls.addWidget(self.b_load); ctrls.addWidget(self.b_play); ctrls.addWidget(self.b_stop); ctrls.addWidget(self.b_dump)
        l_lay.addLayout(ctrls); lay.addWidget(l_cnt); lay.addWidget(self.vol)
        self.tmr = QTimer(); self.tmr.timeout.connect(self.update_ui)
    def dragEnterEvent(self, event): (event.accept() if get_filepath_from_drop(event) else event.ignore())
    def dropEvent(self, event):
        path = get_filepath_from_drop(event)
        if path: self.load_track(path); event.accept()
    def set_v_internal(self, v): self.vol.setValue(v)
    def set_v_engine(self, v): (self.pipeline and self.pipeline.set_property("volume", get_log_volume(v)))
    def toggle_pfl(self):
        self.pfl = not self.pfl; self.lbl_s.setStyleSheet(f"color:{'#ffa500' if self.pfl else '#eee'}; font-size:12px; font-weight:bold; border:{'1px solid #ffa500' if self.pfl else 'none'};")
        self.ar.route_player(self.player_id, self.pfl)
    def toggle_time_mode(self): self.show_remaining = not self.show_remaining; self.update_ui()
    def upd_clip(self, s, e): self.c_in, self.c_out = s, e; (not self.playing) and (setattr(self, 'seek_req', True) or self.wf.set_position(s))
    def load_dialog(self):
        f, _ = QFileDialog.getOpenFileName(self, "Load Audio", "", "Audio (*.mp3 *.wav *.ogg *.flac)")
        if f: self.load_track(f)
    def load_track(self, path):
        self.stop_audio(); self.lbl_s.setText(os.path.basename(path)); self.wf.load_waveform_from_file(path); self.dur = 0
        try:
            disc = GstPbutils.Discoverer.new(10 * Gst.SECOND)
            info = disc.discover_uri(Path(path).as_uri())
            self.dur = info.get_duration(); self.update_ui_label(0); self.wf.set_position(0.0)
        except Exception as e: print(f"Discoverer error: {e}")
        self.pipeline = Gst.ElementFactory.make("playbin", None); self.pipeline.set_property("uri", Path(path).as_uri())
        sink = Gst.ElementFactory.make("pulsesink", None)
        props = Gst.Structure.new_empty("props"); props.set_value("application.name", f"Currycaster_Player_{self.player_id}"); sink.set_property("stream-properties", props)
        target = self.ar.global_cue_sink if self.pfl else self.ar.global_pgm_sink
        if target: sink.set_property("device", target)
        self.pipeline.set_property("audio-sink", sink); self.pipeline.set_property("volume", get_log_volume(self.vol.value()))
        self.pipeline.set_state(Gst.State.PAUSED); self.playing = False; self.seek_req = True
    def toggle_play(self):
        if not self.pipeline: return
        if self.playing:
            self.pipeline.set_state(Gst.State.PAUSED); self.tmr.stop(); self.playing = False; self.b_play.setText("PLAY")
        else:
            if self.seek_req: self.pipeline.set_state(Gst.State.PAUSED); self.enforce_seek()
            self.pipeline.set_state(Gst.State.PLAYING); self.tmr.start(40); self.playing = True; self.b_play.setText("PAUSE")
            self.ar.route_player(self.player_id, self.pfl)
    def enforce_seek(self):
        s, d = self.pipeline.query_duration(Gst.Format.TIME)
        if s: self.dur = d
        if self.dur > 0 and self.c_in > 0: self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(self.c_in * self.dur))
        self.seek_req = False
    def stop_audio(self):
        if not self.pipeline: return
        if self.playing:
            self.pipeline.set_state(Gst.State.PAUSED); self.tmr.stop(); self.playing = False; self.b_play.setText("PLAY")
            self.enforce_seek(); self.wf.set_position(self.c_in); self.update_ui_label(int(self.c_in * self.dur))
        else:
            if self.dur > 0: self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
            self.wf.set_position(0.0); self.update_ui_label(0)
    def dump_track(self):
        if self.pipeline: self.pipeline.set_state(Gst.State.NULL)
        self.tmr.stop(); self.pipeline = None; self.lbl_t.setText("00:00"); self.lbl_s.setText(f"Player {self.player_id}"); self.wf.clear_waveform()
    def seek_audio(self, p):
        if self.pipeline and self.dur > 0:
            if self.playing: self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(p * self.dur))
            else: self.c_in, self.seek_req = p, True
        self.wf.set_position(p)
    def update_ui(self):
        if not self.pipeline: return
        sd, d = self.pipeline.query_duration(Gst.Format.TIME); sp, p = self.pipeline.query_position(Gst.Format.TIME)
        if sd: self.dur = d
        if sp and self.dur > 0:
            if self.seek_req and p < int(self.c_in * self.dur): return
            self.wf.set_position(p / self.dur); self.update_ui_label(p)
            if p >= (int(self.c_out * self.dur) if self.c_out < 1.0 else self.dur): self.stop_audio()
    def update_ui_label(self, p):
        if not self.dur: return
        end_p = int(self.c_out * self.dur) if self.c_out < 1.0 else self.dur
        ts = int((end_p - p if self.show_remaining else p) / 1e9)
        prefix = '-' if self.show_remaining else ''
        self.lbl_t.setText(f"{prefix}{ts//60:02}:{ts%60:02}")
        self.lbl_t.setStyleSheet(f"font-size:24px; font-weight:bold; color:{'#ff5555' if self.show_remaining else '#00bcd4'}; font-family:monospace;")
    def open_menu(self):
        m = QMenu(self); m.addAction("Toggle PFL", self.toggle_pfl); pm, cm = m.addMenu("Program Out"), m.addMenu("Cue Out")
        for s in self.ar.refresh_devices(): pm.addAction(s.description, lambda n=s.name: self.ar.set_program_device(n)); cm.addAction(s.description, lambda n=s.name: self.ar.set_cue_device(n))
        m.exec(self.lbl_s.mapToGlobal(self.lbl_s.rect().center()))

# --- 7. CART BUTTON ---
class CartButton(QFrame):
    playing_triggered, finished_triggered = pyqtSignal(object), pyqtSignal(object)
    def __init__(self, ar, parent=None):
        super().__init__(parent); self.setAcceptDrops(True); self.setFrameShape(QFrame.Shape.StyledPanel); self.setMinimumHeight(100)
        self.ar, self.file, self.pipeline, self.playing = ar, None, None, False
        self.uid = f"Currycaster_Cart_{id(self)}"; self.ar.register_cart_id(self.uid)
        self.c_name, self.c_color = "", "#333333"
        lay = QHBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        self.btn = QPushButton("EMPTY"); self.btn.setSizePolicy(self.btn.sizePolicy().Policy.Expanding, self.btn.sizePolicy().Policy.Expanding)
        self.btn.clicked.connect(self.toggle_play); self.btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.btn.customContextMenuRequested.connect(self.open_context)
        self.vol = QSlider(Qt.Orientation.Vertical); self.vol.setRange(0, 100); self.vol.setValue(100); self.vol.setFixedWidth(12); self.vol.valueChanged.connect(self.upd_vol)
        lay.addWidget(self.btn); lay.addWidget(self.vol)
    def dragEnterEvent(self, event): (event.accept() if get_filepath_from_drop(event) else event.ignore())
    def dropEvent(self, event): path = get_filepath_from_drop(event); (path and self.load_file(path) or event.accept())
    def open_context(self, pos):
        if not self.file: return
        m = QMenu(self); m.addAction("Rename Cart", self.ask_rename); m.addAction("Set Color", self.ask_color); m.addSeparator(); m.addAction("Clear Cart", self.clear_cart); m.exec(self.btn.mapToGlobal(pos))
    def ask_rename(self):
        t, ok = QInputDialog.getText(self, "Rename Cart", "New Name:", text=self.c_name)
        if ok and t: self.c_name = t; self.upd_ui()
    def ask_color(self):
        c = QColorDialog.getColor(initial=QColor(self.c_color), parent=self, title="Cart Color")
        if c.isValid(): self.c_color = c.name(); self.upd_ui()
    def clear_cart(self): self.stop(); self.file, self.c_name, self.c_color = None, "", "#333333"; self.upd_ui()
    def load_file(self, p, n=None, c=None, v=100):
        self.file = p; self.c_name = n or os.path.basename(p); self.c_color = c or self.c_color; self.vol.setValue(v); self.upd_ui()
    def upd_ui(self):
        self.btn.setText(self.c_name if self.file else "EMPTY")
        color = self.c_color if self.file else '#222'; text_color = 'white' if self.file else '#555'; border = '2px solid #00ff00' if self.playing else 'none'
        self.btn.setStyleSheet(f"background:{color}; color:{text_color}; font-weight:bold; font-size:11px; border:{border};")
    def toggle_play(self): self.stop() if self.playing else self.play()
    def play(self):
        if not self.file: return
        self.stop(); self.pipeline = Gst.ElementFactory.make("playbin", None); self.pipeline.set_property("uri", Path(self.file).as_uri())
        sink = Gst.ElementFactory.make("pulsesink", None); props = Gst.Structure.new_empty("props"); props.set_value("application.name", self.uid); sink.set_property("stream-properties", props)
        if self.ar.global_pgm_sink: sink.set_property("device", self.ar.global_pgm_sink)
        self.pipeline.set_property("audio-sink", sink); self.pipeline.set_property("volume", get_log_volume(self.vol.value()))
        bus = self.pipeline.get_bus(); bus.add_signal_watch(); bus.connect("message::eos", lambda b,m: self.stop())
        self.pipeline.set_state(Gst.State.PLAYING); self.playing = True; self.upd_ui(); self.playing_triggered.emit(self)
    def stop(self):
        if self.pipeline: self.pipeline.set_state(Gst.State.NULL); self.pipeline = None
        self.playing = False; self.upd_ui(); self.finished_triggered.emit(self)
    def upd_vol(self): (self.pipeline and self.pipeline.set_property("volume", get_log_volume(self.vol.value())))
    def get_data(self): return {"path": self.file, "name": self.c_name, "color": self.c_color, "vol": self.vol.value()}

# --- 8. MAIN WINDOW & APP ---
class Currycaster(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle("Currycaster Broadcaster (RØDECaster Duo Ready)")
        self.setStyleSheet("background:#111; color:#eee;")
        self.ar = AudioRouter(); self.mm = MidiMapper()
        self.mw = MidiWorker(); self.mw.midi_signal.connect(self.mm.handle); self.mw.start()

        main_wid = QWidget(); self.setCentralWidget(main_wid); main_lay = QVBoxLayout(main_wid)
        player_lay = QHBoxLayout()
        self.players = [PlayerModule(i+1, self.mm, self.ar) for i in range(2)] # 2 Players para la Duo
        for p in self.players: player_lay.addWidget(p)
        main_lay.addLayout(player_lay)

        # Carts
        cart_grid = QGridLayout()
        self.carts = []
        for i in range(12): # 12 Carts (3x4)
            c = CartButton(self.ar); cart_grid.addWidget(c, i//4, i%4); self.carts.append(c)
        main_lay.addLayout(cart_grid)

        # Load Carts Config
        self.load_carts()

    def load_carts(self):
        if os.path.exists(CART_CONFIG_FILE):
            try:
                with open(CART_CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    for i, d in enumerate(data[:len(self.carts)]):
                        if d.get("path"): self.carts[i].load_file(d["path"], d.get("name"), d.get("color"), d.get("vol", 100))
            except: pass

    def closeEvent(self, event):
        # Save Carts
        data = [c.get_data() for c in self.carts]
        with open(CART_CONFIG_FILE, 'w') as f: json.dump(data, f, indent=4)
        self.mw.stop(); event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = Currycaster()
    win.show()
    sys.exit(app.exec())
