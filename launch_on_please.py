# Launch on Please — pick a program, pick a monitor, launch on that screen (maximize/workarea),
# or create a Desktop shortcut that does it headlessly.
# Deps: pip install PySide6 psutil pywin32
# Build: pyinstaller --onefile --windowed --icon=LOP.ico --add-data "LOP.ico;." --name "Launch on Please" launch_on_please.py

import os, sys, time, argparse, subprocess, ctypes
from dataclasses import dataclass
from ctypes import wintypes

import psutil
import win32con, win32gui, win32api, win32process
from win32com.client import Dispatch  # for .lnk

from PySide6 import QtCore, QtGui, QtWidgets

APP_NAME = "Launch on Please"

# ----------------- SETTINGS -----------------
DEFAULT_MODE = "maximize"         # "maximize" | "workarea"
DEFAULT_OBSERVE = 4               # seconds to re-apply placement if app jumps back
POLL_INTERVAL = 0.05              # seconds
RECT_TOL = 3                      # px tolerance for rect change detection
WAIT_TIMEOUT_SEC = 45             # overall wait
STABLE_MS_BEFORE_MOVE = 400       # ms with no rect change before we act
EARLY_MOVE_ON_DETECT = True       # jump as soon as we find a plausible main window
OVERLAY_DURATION_MS = 2000        # how long to show monitor numbers
# --------------------------------------------

# DPI awareness so coordinates = real pixels
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

# Stable identity for taskbar/alt-tab so the icon sticks
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ToxicOrca.LaunchOnPlease")
except Exception:
    pass

def resource_path(rel_path: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base, rel_path)

# Win32 structs
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong),
                ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong)]

# ------------ Monitor helpers ------------
def enum_monitors_sorted():
    monitors = []
    def _cb(hMonitor, hdcMonitor, lprcMonitor, dwData):
        r = ctypes.cast(lprcMonitor, ctypes.POINTER(RECT)).contents
        monitors.append((hMonitor, (r.left, r.top, r.right, r.bottom)))
        return True
    MonitorEnumProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(RECT), wintypes.LPARAM
    )
    ctypes.windll.user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(_cb), 0)
    monitors.sort(key=lambda x: (x[1][0], x[1][1]))  # spatial sort
    return monitors  # [(hmon, (l,t,r,b)), ...]

def workarea_for_hmon(hmon):
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        rw = mi.rcWork
        return (rw.left, rw.top, rw.right, rw.bottom)
    r = RECT()
    SPI_GETWORKAREA = 0x0030
    if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        return (r.left, r.top, r.right, r.bottom)
    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    return (0, 0, sw, sh)

def hmon_from_rect(mr):
    cx = (mr[0] + mr[2]) // 2
    cy = (mr[1] + mr[3]) // 2
    MONITOR_DEFAULTTONEAREST = 2
    pt = wintypes.POINT(cx, cy)
    return ctypes.windll.user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)

# ------------ Window helpers ------------
def rect(hwnd):
    try:
        l,t,r,b = win32gui.GetWindowRect(hwnd)
        return (l,t,r,b)
    except win32gui.error:
        return None

def rect_changed(a, b, tol=RECT_TOL):
    if not a or not b:
        return True
    return any(abs(a[i]-b[i]) > tol for i in range(4))

def is_main_style(hwnd):
    GWL_STYLE, GWL_EXSTYLE = -16, -20
    try:
        style = win32gui.GetWindowLong(hwnd, GWL_STYLE)
        ex = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
    except win32gui.error:
        return False
    WS_OVERLAPPEDWINDOW = (win32con.WS_OVERLAPPED |
                           win32con.WS_CAPTION |
                           win32con.WS_SYSMENU |
                           win32con.WS_THICKFRAME |
                           win32con.WS_MINIMIZEBOX |
                           win32con.WS_MAXIMIZEBOX)
    is_tool = bool(ex & win32con.WS_EX_TOOLWINDOW)
    return (style & WS_OVERLAPPEDWINDOW) and not is_tool

def list_top_windows():
    out = []
    def _enum(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            out.append(hwnd)
    win32gui.EnumWindows(_enum, None)
    return set(out)

def hwnd_pid(hwnd):
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid
    except Exception:
        return None

def hwnd_exe_name(hwnd):
    pid = hwnd_pid(hwnd)
    if not pid:
        return ""
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""

def pick_best_window_by(pid_set=None, exe_base=None, since_handles=None):
    """Pick the best 'main' window by (a) PID set, (b) exe name, or (c) new since snapshot."""
    candidates = []
    def _enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if not is_main_style(hwnd):
            return
        try:
            l,t,r,b = win32gui.GetWindowRect(hwnd)
        except win32gui.error:
            return
        w,h = r-l, b-t
        if w < 200 or h < 150:
            return

        pid = hwnd_pid(hwnd)
        name = hwnd_exe_name(hwnd)
        score = 0
        if pid_set and pid in pid_set:
            score += 1000
        if exe_base and name == exe_base:
            score += 500
        if since_handles and hwnd not in since_handles:
            score += 200  # new window since launch
        if score > 0:
            candidates.append((score, w*h, hwnd))
    win32gui.EnumWindows(_enum, None)
    if not candidates:
        return None
    candidates.sort(key=lambda x:(x[0], x[1]), reverse=True)
    return candidates[0][2]

def wait_for_stable_window(select_fn, timeout=WAIT_TIMEOUT_SEC):
    """
    If EARLY_MOVE_ON_DETECT is True, return the first plausible window
    immediately; otherwise, wait until its rect stops changing for
    STABLE_MS_BEFORE_MOVE.
    """
    deadline = time.time() + timeout
    if EARLY_MOVE_ON_DETECT:
        while time.time() < deadline:
            cand = select_fn()
            if cand:
                return cand
            time.sleep(POLL_INTERVAL)
        return None

    # "stable" wait path
    stable_needed = STABLE_MS_BEFORE_MOVE / 1000.0
    last_rect = None
    stable_time = 0.0
    hwnd = None
    while time.time() < deadline:
        cand = select_fn()
        if not cand:
            time.sleep(POLL_INTERVAL)
            continue
        cur = rect(cand)
        if last_rect is None or cand != hwnd or rect_changed(cur, last_rect):
            hwnd = cand
            last_rect = cur
            stable_time = 0.0
        else:
            stable_time += POLL_INTERVAL
        if stable_time >= stable_needed:
            return hwnd
        time.sleep(POLL_INTERVAL)
    return None

def move_and_set(hwnd, hmon, mode):
    """
    Reliable placement:
      1) Restore to normal (if needed)
      2) For 'maximize'/'workarea': PARK fully inside target monitor at medium size,
         then maximize/fill. For 'normal': move to target monitor but keep current size.
    """
    placement = win32gui.GetWindowPlacement(hwnd)
    was_maximized = (placement[1] == win32con.SW_SHOWMAXIMIZED)

    # Ensure normal state so SetWindowPos is honored consistently
    if was_maximized:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.05)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)

    wa_l, wa_t, wa_r, wa_b = workarea_for_hmon(hmon)
    wa_w, wa_h = (wa_r - wa_l), (wa_b - wa_t)

    if mode == "normal":
        # Keep current size, just move fully onto target monitor's work area.
        cur = rect(hwnd)
        if not cur:
            cur_w, cur_h = 1000, 700
        else:
            cur_w = max(300, min(cur[2]-cur[0], wa_w - 40))
            cur_h = max(200, min(cur[3]-cur[1], wa_h - 40))
        target_x = wa_l + 20
        target_y = wa_t + 20
        win32gui.SetWindowPos(
            hwnd, None, target_x, target_y, cur_w, cur_h,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED
        )
        return

    # For maximize/workarea: park first so Windows associates this monitor
    park_w = min(1200, max(800, wa_w - 200))
    park_h = min(900,  max(600, wa_h - 200))
    park_x = wa_l + max(20, (wa_w - park_w)//2)
    park_y = wa_t + max(20, (wa_h - park_h)//3)

    win32gui.SetWindowPos(
        hwnd, None, park_x, park_y, park_w, park_h,
        win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED
    )
    time.sleep(0.02)

    if mode == "maximize":
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    else:  # "workarea"
        win32gui.SetWindowPos(
            hwnd, None, wa_l, wa_t, wa_w, wa_h,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED
        )

def ensure_on_target(hwnd, hmon_target, mode, seconds):
    until = time.time() + max(0, int(seconds))
    last = rect(hwnd)
    while time.time() < until:
        try:
            if not (hwnd and win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)):
                return
        except win32gui.error:
            return
        cur_hmon = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
        cur_rect = rect(hwnd)
        if cur_hmon != hmon_target or rect_changed(cur_rect, last):
            move_and_set(hwnd, hmon_target, mode)
            cur_rect = rect(hwnd)
        last = cur_rect
        time.sleep(POLL_INTERVAL)

# ------------- Headless core -------------
def run_headless(exe_path, monitor_index, mode, observe_seconds):
    mons = enum_monitors_sorted()
    if not mons:
        raise RuntimeError("No monitors detected.")
    if monitor_index < 0 or monitor_index >= len(mons):
        raise RuntimeError(f"Monitor index {monitor_index} out of range (found {len(mons)}).")
    hmon_target, _ = mons[monitor_index]

    before = list_top_windows()
    # no console window for spawned app
    proc = subprocess.Popen(
        [exe_path],
        close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    try:
        parent = psutil.Process(proc.pid)
    except Exception as e:
        raise RuntimeError(f"Failed to inspect launched process: {e}")

    # Faster child discovery (~0.75s total)
    pid_set = {parent.pid}
    exe_base = os.path.basename(exe_path).lower()
    for _ in range(15):  # 15 * 0.05 = 0.75s
        try:
            for c in parent.children(recursive=True):
                pid_set.add(c.pid)
        except Exception:
            pass
        time.sleep(0.05)

    def select():
        return pick_best_window_by(pid_set=pid_set, exe_base=exe_base, since_handles=before)

    hwnd = wait_for_stable_window(select, timeout=WAIT_TIMEOUT_SEC)
    if not hwnd:
        def select_new_only():
            return pick_best_window_by(pid_set=None, exe_base=None, since_handles=before)
        hwnd = wait_for_stable_window(select_new_only, timeout=10)

    if not hwnd:
        raise RuntimeError("Could not find a stable main window for the launched app.")

    move_and_set(hwnd, hmon_target, mode)
    ensure_on_target(hwnd, hmon_target, mode, observe_seconds)

# ------------- Desktop Shortcut helpers -------------
def get_desktop_dir():
    try:
        from win32com.shell import shell, shellcon
        return shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, 0, 0)
    except Exception:
        return os.path.join(os.path.expanduser("~"), "Desktop")

def is_frozen():
    return getattr(sys, "frozen", False)

def current_launcher_target_and_args():
    if is_frozen():
        return sys.executable, []  # this EXE
    # Use pythonw.exe to avoid console window
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable
    return pythonw, [os.path.abspath(sys.argv[0])]

def create_desktop_shortcut(shortcut_name, exe_to_launch, monitor_index, mode, observe_seconds):
    desktop = get_desktop_dir()
    lnk_path = os.path.join(desktop, f"{shortcut_name}.lnk")
    target, base_args = current_launcher_target_and_args()
    args = base_args + [
        "--exe", exe_to_launch,
        "--monitor", str(monitor_index),
        "--mode", mode,
        "--observe", str(observe_seconds)
    ]
    arguments = " ".join(f'"{a}"' if " " in a else a for a in args)

    shell = Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(lnk_path)
    shortcut.TargetPath = target
    shortcut.Arguments = arguments
    shortcut.WorkingDirectory = os.path.dirname(exe_to_launch) or os.getcwd()

    # 👇 Use the TARGET PROGRAM'S icon (index 0)
    try:
        shortcut.IconLocation = f"{exe_to_launch},0"
    except Exception:
        pass

    shortcut.Save()
    return lnk_path


# ------------- Overlay (monitor numbers) -------------
class MonitorOverlay(QtWidgets.QWidget):
    def __init__(self, rect, number, parent=None):
        super().__init__(parent)
        self.number = str(number)
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint |
            QtCore.Qt.WindowStaysOnTopHint |
            QtCore.Qt.Tool
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        l, t, r, b = rect
        self.setGeometry(l, t, r - l, b - t)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 90))
        w = self.width()
        h = self.height()
        d = int(min(w, h) * 0.3)
        cx = w // 2
        cy = h // 2
        circle_rect = QtCore.QRect(cx - d//2, cy - d//2, d, d)
        brush = QtGui.QBrush(QtGui.QColor(40, 120, 255, 210))
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 230))
        pen.setWidth(4)
        p.setPen(pen)
        p.setBrush(brush)
        p.drawEllipse(circle_rect)
        font = QtGui.QFont()
        font.setPointSize(int(d * 0.45))
        font.setBold(True)
        p.setFont(font)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255)))
        p.drawText(self.rect(), QtCore.Qt.AlignCenter, self.number)

# ------------- GUI -------------
@dataclass
class MonitorItem:
    index: int
    hmon: int
    rect: tuple
    label: str

class LaunchWorker(QtCore.QThread):
    done = QtCore.Signal(str)
    error = QtCore.Signal(str)
    def __init__(self, exe_path, monitor_index, mode, observe):
        super().__init__()
        self.exe_path = exe_path
        self.monitor_index = monitor_index
        self.mode = mode
        self.observe = observe
    def run(self):
        try:
            run_headless(self.exe_path, self.monitor_index, self.mode, self.observe)
            self.done.emit("Launched and placed successfully.")
        except Exception as e:
            self.error.emit(str(e))

class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumWidth(620)
        self._apply_dark_palette()
        # set window icon (taskbar/titlebar)
        self.setWindowIcon(QtGui.QIcon(resource_path("LOP.ico")))

        self.exe_edit = QtWidgets.QLineEdit()
        self.exe_edit.setPlaceholderText("C:\\Path\\To\\Program.exe")
        browse_btn = QtWidgets.QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)

        exe_row = QtWidgets.QHBoxLayout()
        exe_row.addWidget(self.exe_edit, 1)
        exe_row.addWidget(browse_btn)

        self.monitor_combo = QtWidgets.QComboBox()
        self._load_monitors()

        show_nums_btn = QtWidgets.QPushButton("Show Numbers (2s)")
        show_nums_btn.clicked.connect(self._show_monitor_numbers)

        monitor_row = QtWidgets.QHBoxLayout()
        monitor_row.addWidget(self.monitor_combo, 1)
        monitor_row.addWidget(show_nums_btn)

        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems([
            "Maximize (recommended)",
            "Fit to work area",
            "Normal window"
        ])
        self.mode_combo.setCurrentIndex(0)

        self.observe_spin = QtWidgets.QSpinBox()
        self.observe_spin.setRange(0, 30)
        self.observe_spin.setValue(DEFAULT_OBSERVE)
        self.observe_spin.setSuffix(" s")
        self.observe_spin.setToolTip("After launching, keep watch and correct if the app tries to jump back.")

        self.launch_btn = QtWidgets.QPushButton("Launch Now")
        self.launch_btn.clicked.connect(self._on_launch_now)

        self.shortcut_btn = QtWidgets.QPushButton("Create Desktop Shortcut")
        self.shortcut_btn.clicked.connect(self._on_create_shortcut)

        form = QtWidgets.QFormLayout()
        form.addRow("Program:", exe_row)
        form.addRow("Monitor:", monitor_row)
        form.addRow("Behavior:", self.mode_combo)
        form.addRow("Watch & correct:", self.observe_spin)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.launch_btn)
        btn_row.addWidget(self.shortcut_btn)

        root = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel(f"<b>{APP_NAME}</b>")
        title.setAlignment(QtCore.Qt.AlignHCenter)
        root.addWidget(title)
        root.addLayout(form)
        root.addSpacing(6)
        root.addLayout(btn_row)

        # --- footer ---
        footer = QtWidgets.QLabel("By Toxic Orca Studio")
        footer.setAlignment(QtCore.Qt.AlignHCenter)
        footer.setStyleSheet("color: rgb(150, 150, 150); font-size: 10pt; margin-top: 12px;")
        root.addWidget(footer)

        self._overlays = []
        self.worker = None

    def _apply_dark_palette(self):
        app = QtWidgets.QApplication.instance()
        palette = QtGui.QPalette()
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor(32, 32, 36))
        palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor(230, 230, 235))
        palette.setColor(QtGui.QPalette.Base, QtGui.QColor(22, 22, 26))
        palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(32, 32, 36))
        palette.setColor(QtGui.QPalette.Text, QtGui.QColor(230, 230, 235))
        palette.setColor(QtGui.QPalette.Button, QtGui.QColor(45, 45, 50))
        palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(230, 230, 235))
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(66, 133, 244))
        palette.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.white)
        app.setPalette(palette)
        app.setStyle("Fusion")

    def _load_monitors(self):
        self.monitor_combo.clear()
        self.monitors = []
        for i, (hmon, mr) in enumerate(enum_monitors_sorted()):
            w, h = mr[2]-mr[0], mr[3]-mr[1]
            label = f"{i}: {w}×{h} @ ({mr[0]},{mr[1]})"
            self.monitors.append(MonitorItem(i, hmon, mr, label))
            self.monitor_combo.addItem(label)

    def _browse(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select Program", "", "Programs (*.exe);;All Files (*.*)"
        )
        if path:
            self.exe_edit.setText(path)

    def _error(self, msg):
        QtWidgets.QMessageBox.critical(self, APP_NAME, msg)

    def _info(self, msg):
        QtWidgets.QMessageBox.information(self, APP_NAME, msg)

    def _selected_mode(self):
        idx = self.mode_combo.currentIndex()
        return ["maximize", "workarea", "normal"][idx]

    def _on_launch_now(self):
        exe = self.exe_edit.text().strip().strip('"')
        if not exe or not os.path.isfile(exe):
            self._error("Please select a valid .exe file.")
            return
        idx = self.monitor_combo.currentIndex()
        if idx < 0 or idx >= len(self.monitors):
            self._error("Please select a monitor.")
            return
        mode = self._selected_mode()
        observe = int(self.observe_spin.value())

        self.launch_btn.setEnabled(False)
        self.shortcut_btn.setEnabled(False)
        self.worker = LaunchWorker(exe, idx, mode, observe)
        self.worker.done.connect(lambda m: (self._info(m), self._enable_buttons()))
        self.worker.error.connect(lambda e: (self._error(e), self._enable_buttons()))
        self.worker.start()

    def _enable_buttons(self):
        self.launch_btn.setEnabled(True)
        self.shortcut_btn.setEnabled(True)

    def _on_create_shortcut(self):
        exe = self.exe_edit.text().strip().strip('"')
        if not exe or not os.path.isfile(exe):
            self._error("Please select a valid .exe file.")
            return
        idx = self.monitor_combo.currentIndex()
        if idx < 0 or idx >= len(self.monitors):
            self._error("Please select a monitor.")
            return
        mode = self._selected_mode()
        observe = int(self.observe_spin.value())

        app_name = os.path.splitext(os.path.basename(exe))[0]
        shortcut_name = f"{app_name} - LOP"
        try:
            lnk = create_desktop_shortcut(shortcut_name, exe, idx, mode, observe)
        except Exception as e:
            self._error(f"Failed to create shortcut:\n{e}")
            return
        self._info(f"Shortcut created:\n{lnk}\n\nUse that icon next time — it launches on the selected monitor.")

    # --- show big numbers on each monitor for a moment ---
    def _show_monitor_numbers(self):
        for w in getattr(self, "_overlays", []):
            w.close()
        self._overlays = []
        if not hasattr(self, "monitors") or not self.monitors:
            self._load_monitors()
        for m in self.monitors:
            ov = MonitorOverlay(m.rect, m.index)
            ov.show()
            self._overlays.append(ov)
        QtCore.QTimer.singleShot(OVERLAY_DURATION_MS, self._dismiss_overlays)

    def _dismiss_overlays(self):
        for w in self._overlays:
            try:
                w.close()
            except Exception:
                pass
        self._overlays.clear()

# ------------- CLI -------------
def parse_args():
    p = argparse.ArgumentParser(description=APP_NAME)
    p.add_argument("--exe", help="Path to target program .exe")
    p.add_argument("--monitor", type=int, help="Monitor index (0-based left→right)")
    p.add_argument("--mode", choices=["maximize", "workarea", "normal"], default=DEFAULT_MODE)
    p.add_argument("--observe", type=int, default=DEFAULT_OBSERVE)
    return p.parse_args()

def main():
    args = parse_args()
    if args.exe:
        exe = os.path.abspath(args.exe)
        if not os.path.isfile(exe):
            print("Invalid --exe path.", file=sys.stderr); sys.exit(2)
        if args.monitor is None:
            print("Missing --monitor (0-based).", file=sys.stderr); sys.exit(2)
        try:
            run_headless(exe, args.monitor, args.mode, args.observe)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr); sys.exit(1)
        sys.exit(0)

    app = QtWidgets.QApplication(sys.argv)
    # app/taskbar icon
    app.setWindowIcon(QtGui.QIcon(resource_path("LOP.ico")))

    w = MainWindow()
    w.resize(660, 280)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
