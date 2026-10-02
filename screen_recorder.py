"""High-performance screen recorder for Windows.

Design goals:
- Real-time encode via ffmpeg pipe (no long freeze when stopping)
- Multi-monitor + window targets
- Mic + system loopback mix
- Cursor highlight drawn on frames
- Optional preview frames for UI
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
import sounddevice as sd

try:
    import mss
except ImportError:
    mss = None  # type: ignore

# Windows GDI window capture — optional (not available on macOS)
if sys.platform == "win32":
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32gui  # type: ignore
    import win32ui  # type: ignore
else:
    win32api = None  # type: ignore
    win32con = None  # type: ignore
    win32gui = None  # type: ignore
    win32ui = None  # type: ignore


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def get_monitors() -> list[dict]:
    """Return list of monitors: {id, title, left, top, width, height}."""
    out: list[dict] = []
    if mss is not None:
        with mss.mss() as sct:
            for i, mon in enumerate(sct.monitors):
                # monitors[0] is virtual desktop spanning all
                if i == 0:
                    out.append(
                        {
                            "id": "all",
                            "hwnd": 0,
                            "kind": "screen",
                            "title": f"全部显示器 ({mon['width']}×{mon['height']})",
                            "left": mon["left"],
                            "top": mon["top"],
                            "width": mon["width"],
                            "height": mon["height"],
                        }
                    )
                else:
                    out.append(
                        {
                            "id": f"mon{i}",
                            "hwnd": 0,
                            "kind": "screen",
                            "title": f"显示器 {i} ({mon['width']}×{mon['height']})",
                            "left": mon["left"],
                            "top": mon["top"],
                            "width": mon["width"],
                            "height": mon["height"],
                        }
                    )
        return out
    # Fallback single screen
    if win32api is not None and win32con is not None:
        w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
    else:
        w, h = 1920, 1080
    return [
        {
            "id": "all",
            "hwnd": 0,
            "kind": "screen",
            "title": f"整个屏幕 ({w}×{h})",
            "left": 0,
            "top": 0,
            "width": w,
            "height": h,
        }
    ]


def get_window_list(*, browsers_only: bool = False) -> list[dict]:
    """Visible top-level windows with title/hwnd/class (Windows). Empty on other OS."""
    if win32gui is None:
        return []
    browser_classes = {
        "Chrome_WidgetWin_1",
        "MozillaWindowClass",
        "ApplicationFrameWindow",  # Edge UWP shell
    }
    browser_title_keys = ("chrome", "edge", "firefox", "brave", "opera", "vivaldi", "360", "浏览器", "browser")

    windows: list[dict] = []

    def enum_win(hwnd, result):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        if not title.strip():
            return
        try:
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            if style & win32con.WS_CHILD:
                return
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            if w < 120 or h < 80:
                return
            cls = win32gui.GetClassName(hwnd) or ""
            if cls in ("Progman", "Shell_TrayWnd", "WorkerW", "Button"):
                return
            title_l = title.lower()
            is_browser = cls in browser_classes or any(k in title_l for k in browser_title_keys)
            if browsers_only and not is_browser:
                return
            kind = "browser" if is_browser else "window"
            prefix = "🌐 " if is_browser else "🗔 "
            result.append(
                {
                    "id": f"hwnd:{hwnd}",
                    "hwnd": int(hwnd),
                    "kind": kind,
                    "title": prefix + title[:80],
                    "class": cls,
                    "left": rect[0],
                    "top": rect[1],
                    "width": w,
                    "height": h,
                }
            )
        except Exception:
            return

    try:
        win32gui.EnumWindows(enum_win, windows)
    except Exception:
        pass
    windows.sort(key=lambda x: x["title"].lower())
    return windows


def get_capture_targets() -> list[dict]:
    """Screens + windows for UI combo."""
    return get_monitors() + get_window_list(browsers_only=False)


def get_audio_devices() -> tuple[list[dict], list[dict]]:
    """Return (mics, system_loopback_candidates). Prefer MME/WASAPI Realtek over mapper/BT HF."""
    mics: list[dict] = []
    systems: list[dict] = []
    try:
        devices = sd.query_devices()
        for idx, d in enumerate(devices):
            if int(d.get("max_input_channels") or 0) <= 0:
                continue
            name = str(d.get("name") or f"Device {idx}")
            try:
                host = str(sd.query_hostapis(d["hostapi"])["name"] or "")
            except Exception:
                host = ""
            rate = float(d.get("default_samplerate") or 0)
            item = {
                "index": idx,
                "name": f"{name} ({host})" if host else name,
                "host": host,
                "rate": rate,
            }
            name_l = name.lower()
            if any(
                k in name_l
                for k in (
                    "立体声",
                    "混音",
                    "mix",
                    "loopback",
                    "cable",
                    "virtual",
                    "what u hear",
                    "stereo mix",
                )
            ):
                systems.append(item)
            else:
                # Skip broken ultra-low-rate Bluetooth hands-free capture endpoints
                if rate and rate < 16000 and ("hands-free" in name_l or "ag audio" in name_l or "bthhf" in name_l):
                    continue
                mics.append(item)

        def _rank(item: dict) -> tuple:
            host = str(item.get("host") or "").lower()
            name = str(item.get("name") or "").lower()
            # Prefer MME / WASAPI Realtek mic; demote mapper / primary driver aliases
            host_score = 0
            if "mme" in host:
                host_score = 0
            elif "wasapi" in host:
                host_score = 1
            elif "directsound" in host:
                host_score = 2
            else:
                host_score = 3
            alias = 1 if any(k in name for k in ("sound mapper", "主声音捕获", "primary")) else 0
            return (alias, host_score, name)

        mics.sort(key=_rank)
        systems.sort(key=_rank)
    except Exception as exc:
        print(f"audio device query failed: {exc}", flush=True)
    return mics, systems


class MicTester:
    """Live mic level + short buffer for debug (pick the right input device)."""

    def __init__(
        self,
        *,
        on_level: Callable[[float], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        sample_rate: int = 44100,
        buffer_seconds: float = 4.0,
    ) -> None:
        self.on_level = on_level or (lambda _v: None)
        self.on_status = on_status or (lambda _m: None)
        self.sample_rate = int(sample_rate)
        self.buffer_seconds = float(buffer_seconds)
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._max_samples = max(1, int(self.sample_rate * self.buffer_seconds))
        self._playing = False
        self.device_idx: int | None = None

    @property
    def is_running(self) -> bool:
        return self._stream is not None

    def start(self, device_idx: int) -> None:
        self.stop()
        self.device_idx = int(device_idx)
        self._chunks = []
        try:
            info = sd.query_devices(self.device_idx)
            ch = max(1, min(2, int(info.get("max_input_channels") or 1)))
        except Exception:
            ch = 1

        def _cb(indata, frames, time_info, status):  # noqa: ARG001
            if status:
                pass
            try:
                mono = np.asarray(indata, dtype=np.float32)
                if mono.ndim > 1:
                    mono = mono.mean(axis=1)
                peak = float(np.max(np.abs(mono))) if mono.size else 0.0
                # Map peak to 0..100 with mild log feel for quiet speech
                level = min(100.0, peak * 140.0)
                self.on_level(level)
                with self._lock:
                    self._chunks.append(mono.copy())
                    total = sum(c.size for c in self._chunks)
                    while total > self._max_samples and self._chunks:
                        dropped = self._chunks.pop(0)
                        total -= dropped.size
            except Exception:
                pass

        try:
            self._stream = sd.InputStream(
                device=self.device_idx,
                channels=ch,
                samplerate=self.sample_rate,
                dtype="float32",
                blocksize=1024,
                callback=_cb,
            )
            self._stream.start()
            name = ""
            try:
                name = str(sd.query_devices(self.device_idx).get("name") or "")
            except Exception:
                pass
            self.on_status(f"🎤 试听中：{name or f'设备 {self.device_idx}'} — 请对着说话看电平")
        except Exception as exc:
            self._stream = None
            self.on_status(f"无法打开麦克风：{exc}")
            raise

    def stop(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self.on_level(0.0)

    def snapshot_audio(self) -> np.ndarray | None:
        with self._lock:
            if not self._chunks:
                return None
            return np.concatenate(self._chunks).astype(np.float32, copy=False)

    def play_buffer_async(self) -> None:
        """Play the last few seconds captured during the test (for device debug)."""
        if self._playing:
            self.on_status("正在回放，请稍候…")
            return
        data = self.snapshot_audio()
        if data is None or data.size < self.sample_rate // 10:
            self.on_status("缓冲太短：请先点「开始试听」并说几句话，再回放")
            return

        def _run() -> None:
            self._playing = True
            try:
                # stop input briefly to avoid feedback if speakers loud
                was = self.is_running
                dev = self.device_idx
                if was:
                    self.stop()
                self.on_status(f"▶ 回放约 {data.size / self.sample_rate:.1f}s…")
                sd.play(data, self.sample_rate, blocking=True)
                self.on_status("✅ 回放结束。有声音 = 该输入可用")
                if was and dev is not None:
                    try:
                        self.start(dev)
                    except Exception:
                        pass
            except Exception as exc:
                self.on_status(f"回放失败：{exc}")
            finally:
                self._playing = False

        threading.Thread(target=_run, daemon=True).start()


def _ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _silent_subprocess_kwargs() -> dict:
    """Hide console window for ffmpeg (console-subsystem) on Windows."""
    if sys.platform != "win32":
        return {}
    # CREATE_NO_WINDOW = 0x08000000 — no black cmd flash while recording/muxing
    kw: dict = {"creationflags": 0x08000000}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kw["startupinfo"] = si
    except Exception:
        pass
    return kw


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def _mss_instance():
    """Reuse one mss handle per thread — creating mss every frame is a major lag source."""
    sct = getattr(_thread_local, "sct", None)
    if sct is None:
        sct = mss.mss()
        _thread_local.sct = sct
    return sct


def _bgr_is_blank(frame: np.ndarray | None) -> bool:
    """Detect all-black / all-white frames (typical without Screen Recording permission)."""
    if frame is None or frame.size == 0:
        return True
    sample = frame[:: max(1, frame.shape[0] // 48), :: max(1, frame.shape[1] // 48)]
    if sample.size == 0:
        return True
    mean = float(sample.mean())
    return mean < 3.0 or mean > 252.0


def _capture_bgr_screencapture(region: dict | None) -> np.ndarray | None:
    """One-shot macOS screencapture → BGR (slow; for permission probe / fallback only)."""
    if sys.platform != "darwin":
        return None
    path = Path(tempfile.mkdtemp(prefix="dt_rec_")) / "f.png"
    cmd = ["screencapture", "-x", "-t", "png"]
    if region:
        cmd += [
            "-R",
            f"{int(region['left'])},{int(region['top'])},{int(region['width'])},{int(region['height'])}",
        ]
    cmd.append(str(path))
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        if r.returncode != 0 or not path.is_file():
            return None
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        try:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except Exception:
            pass
        if img is None or _bgr_is_blank(img):
            return None
        return img
    except Exception:
        return None


def _request_mac_screen_capture_access() -> None:
    """Ask macOS to show the Screen Recording permission dialog (10.15+)."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        lib_name = ctypes.util.find_library("CoreGraphics")
        if not lib_name:
            return
        cg = ctypes.cdll.LoadLibrary(lib_name)
        # bool CGRequestScreenCaptureAccess(void);
        if hasattr(cg, "CGRequestScreenCaptureAccess"):
            cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
            cg.CGRequestScreenCaptureAccess()
    except Exception:
        pass


def capture_bgr(
    region: dict | None = None,
    *,
    allow_slow_fallback: bool = False,
) -> np.ndarray | None:
    """Capture region as BGR uint8 array. region: left,top,width,height or None for primary.

    ``allow_slow_fallback`` enables the macOS ``screencapture`` CLI path. Keep it
    False in the recording / preview loops — that subprocess can take seconds and
    freezes the UI if called every frame when mss returns a blank (no TCC grant).
    """
    if mss is not None:
        try:
            sct = _mss_instance()
            if region is None:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                shot = sct.grab(mon)
            else:
                shot = sct.grab(
                    {
                        "left": int(region["left"]),
                        "top": int(region["top"]),
                        "width": max(2, int(region["width"])),
                        "height": max(2, int(region["height"])),
                    }
                )
            # BGRA -> BGR contiguous
            arr = np.asarray(shot)  # HxWx4 BGRA
            bgr = np.ascontiguousarray(arr[:, :, :3])
            if not _bgr_is_blank(bgr):
                return bgr
            # Blank often means missing Screen Recording permission on macOS
            print("mss capture blank (permission?)", flush=True)
        except Exception as exc:
            # Reset broken mss handle then fall through
            try:
                _thread_local.sct = None
            except Exception:
                pass
            print(f"mss capture failed: {exc}", flush=True)

    # Slow path: only for one-shot permission probes — never per-frame
    if allow_slow_fallback and sys.platform == "darwin":
        img = _capture_bgr_screencapture(region)
        if img is not None:
            return img

    # GDI fallback (Windows only)
    if win32api is None or win32gui is None or win32ui is None or win32con is None:
        return None
    try:
        if region:
            left, top = int(region["left"]), int(region["top"])
            w, h = max(2, int(region["width"])), max(2, int(region["height"]))
        else:
            left = top = 0
            w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        hwnd = win32gui.GetDesktopWindow()
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        save_dc.BitBlt((0, 0), (w, h), mfc_dc, (left, top), win32con.SRCCOPY)
        bits = bmp.GetBitmapBits(True)
        img = np.frombuffer(bits, dtype=np.uint8).reshape((h, w, 4))
        bgr = img[:, :, :3].copy()
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        return bgr
    except Exception as exc:
        print(f"gdi capture failed: {exc}", flush=True)
        return None


def resolve_region(target: dict | None) -> dict | None:
    """Compute live capture region from a target dict (screen or hwnd)."""
    if not target:
        return None
    hwnd = int(target.get("hwnd") or 0)
    if hwnd > 0 and win32gui is not None and win32gui.IsWindow(hwnd):
        try:
            # Prefer restored geometry
            rect = win32gui.GetWindowRect(hwnd)
            left, top, right, bottom = rect
            return {
                "left": left,
                "top": top,
                "width": max(2, right - left),
                "height": max(2, bottom - top),
            }
        except Exception:
            pass
    # Screen region stored on target
    if all(k in target for k in ("left", "top", "width", "height")):
        return {
            "left": int(target["left"]),
            "top": int(target["top"]),
            "width": max(2, int(target["width"])),
            "height": max(2, int(target["height"])),
        }
    return None


def screen_capture_permission_ok(target: dict | None = None) -> tuple[bool, str]:
    """Probe whether screen pixels can be captured (macOS Screen Recording TCC)."""
    region = resolve_region(target) if target else None
    # Fast path first (mss / GDI) — does not spawn screencapture
    frame = capture_bgr(region, allow_slow_fallback=False)
    if frame is not None and not _bgr_is_blank(frame):
        return True, ""
    if sys.platform == "darwin":
        _request_mac_screen_capture_access()
        # One-shot CLI capture triggers / uses the TCC grant
        frame = capture_bgr(region, allow_slow_fallback=True)
        if frame is not None and not _bgr_is_blank(frame):
            return True, ""
        return (
            False,
            "无法捕获屏幕画面（多为白屏/黑屏）。请打开「系统设置 → 隐私与安全 → 屏幕录制」，"
            "勾选 Desktop Toolkit 后完全退出应用再重试。",
        )
    return False, "无法捕获屏幕画面，请检查显示权限后重试。"


def draw_cursor_highlight(
    frame_bgr: np.ndarray,
    region: dict | None,
    *,
    color_bgr: tuple[int, int, int] = (0, 255, 255),
    radius: int = 22,
    show_pointer: bool = True,
) -> None:
    """Draw cursor highlight in-place (BGR)."""
    try:
        if win32gui is not None:
            cx, cy = win32gui.GetCursorPos()
        else:
            # macOS / others: best-effort via Qt if available
            from PyQt6.QtGui import QCursor

            p = QCursor.pos()
            cx, cy = int(p.x()), int(p.y())
    except Exception:
        return
    if region:
        rx = cx - int(region["left"])
        ry = cy - int(region["top"])
    else:
        rx, ry = cx, cy
    h, w = frame_bgr.shape[:2]
    if rx < -radius or ry < -radius or rx > w + radius or ry > h + radius:
        return
    # Cheap ring + tip (avoid full-frame copy + addWeighted which lagged UI)
    cv2.circle(frame_bgr, (int(rx), int(ry)), radius, color_bgr, 2, lineType=cv2.LINE_AA)
    cv2.circle(frame_bgr, (int(rx), int(ry)), max(3, radius // 4), color_bgr, -1, lineType=cv2.LINE_AA)
    if show_pointer:
        pts = np.array(
            [[rx, ry], [rx + 14, ry + 14], [rx + 4, ry + 16]],
            dtype=np.int32,
        )
        cv2.fillPoly(frame_bgr, [pts], (255, 255, 255))
        cv2.polylines(frame_bgr, [pts], True, (0, 0, 0), 1, lineType=cv2.LINE_AA)


def blend_overlay_rgba(frame_bgr: np.ndarray, overlay_rgba: np.ndarray | None) -> np.ndarray:
    """Alpha-blend RGBA overlay onto BGR frame (fast path for sparse brush strokes)."""
    if overlay_rgba is None:
        return frame_bgr
    try:
        if overlay_rgba.shape[0] != frame_bgr.shape[0] or overlay_rgba.shape[1] != frame_bgr.shape[1]:
            overlay_rgba = cv2.resize(
                overlay_rgba,
                (frame_bgr.shape[1], frame_bgr.shape[0]),
                interpolation=cv2.INTER_NEAREST,
            )
        if overlay_rgba.shape[2] < 4:
            return frame_bgr
        alpha_u8 = overlay_rgba[:, :, 3]
        if int(alpha_u8.max()) < 3:
            return frame_bgr
        # Only blend nonzero alpha pixels (brush overlays are sparse)
        mask = alpha_u8 > 2
        if not np.any(mask):
            return frame_bgr
        a = alpha_u8[mask].astype(np.float32) * (1.0 / 255.0)
        # overlay stored RGBA; convert RGB->BGR for those pixels
        src = overlay_rgba[:, :, :3][:, :, ::-1]
        out = frame_bgr
        for c in range(3):
            base = out[:, :, c][mask].astype(np.float32)
            out[:, :, c][mask] = (base * (1.0 - a) + src[:, :, c][mask].astype(np.float32) * a).astype(np.uint8)
        return out
    except Exception:
        return frame_bgr


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

class AudioRecorder:
    def __init__(self, mic_idx, sys_idx, output_wav_path: str, sample_rate: int = 44100):
        self.mic_idx = mic_idx
        self.sys_idx = sys_idx
        self.output_wav_path = output_wav_path
        self.sample_rate = sample_rate
        self.q_mic: queue.Queue = queue.Queue()
        self.q_sys: queue.Queue = queue.Queue()
        self.is_recording = False
        self.is_paused = False
        self.stream_mic = None
        self.stream_sys = None
        self.wav_file = None
        self.write_thread = None

    def _open_input(self, device_idx: int, put_q: queue.Queue) -> tuple[object | None, int | None]:
        """Open an input stream; try device-native rate/channels before falling back."""
        info = {}
        try:
            info = sd.query_devices(device_idx) or {}
        except Exception:
            pass
        max_ch = max(1, min(2, int(info.get("max_input_channels") or 1)))
        native_rate = int(float(info.get("default_samplerate") or self.sample_rate) or self.sample_rate)
        rates = []
        for r in (self.sample_rate, native_rate, 48000, 44100, 16000):
            if r and r not in rates:
                rates.append(int(r))
        last_err = ""
        for rate in rates:
            for ch in (max_ch, 1, 2):
                if ch > max_ch:
                    continue
                try:
                    def cb(data, frames, t, status, _q=put_q):
                        if not self.is_paused:
                            _q.put(data.copy())

                    stream = sd.InputStream(
                        device=device_idx,
                        channels=ch,
                        samplerate=rate,
                        blocksize=1024,
                        callback=cb,
                    )
                    stream.start()
                    # Keep writer sample rate aligned with the first successful stream
                    self.sample_rate = rate
                    return stream, device_idx
                except Exception as e:
                    last_err = str(e)
                    continue
        print(f"audio open failed device={device_idx}: {last_err}", flush=True)
        return None, None

    def start(self) -> None:
        self.is_recording = True
        self.is_paused = False
        if self.mic_idx is not None:
            self.stream_mic, self.mic_idx = self._open_input(int(self.mic_idx), self.q_mic)

        if self.sys_idx is not None:
            self.stream_sys, self.sys_idx = self._open_input(int(self.sys_idx), self.q_sys)

        if self.mic_idx is not None or self.sys_idx is not None:
            self.wav_file = wave.open(self.output_wav_path, "wb")
            self.wav_file.setnchannels(2)
            self.wav_file.setsampwidth(2)
            self.wav_file.setframerate(self.sample_rate)
            self.write_thread = threading.Thread(target=self._write_loop, daemon=True)
            self.write_thread.start()
        else:
            print("audio recorder: no usable mic/system device", flush=True)

    def _write_loop(self) -> None:
        while self.is_recording or not self.q_mic.empty() or not self.q_sys.empty():
            chunk_mic = None
            chunk_sys = None
            try:
                if self.mic_idx is not None:
                    chunk_mic = self.q_mic.get(timeout=0.04)
            except queue.Empty:
                pass
            try:
                if self.sys_idx is not None:
                    chunk_sys = self.q_sys.get(timeout=0.04)
            except queue.Empty:
                pass
            if chunk_mic is None and chunk_sys is None:
                time.sleep(0.005)
                continue
            if self.wav_file is None:
                continue
            try:
                if chunk_mic is not None and chunk_sys is None:
                    stereo = np.column_stack((chunk_mic[:, 0], chunk_mic[:, 0]))
                elif chunk_sys is not None and chunk_mic is None:
                    if chunk_sys.ndim == 1:
                        stereo = np.column_stack((chunk_sys, chunk_sys))
                    elif chunk_sys.shape[1] == 1:
                        stereo = np.column_stack((chunk_sys[:, 0], chunk_sys[:, 0]))
                    else:
                        stereo = chunk_sys[:, :2]
                else:
                    m = chunk_mic[:, 0] if chunk_mic.ndim > 1 else chunk_mic
                    if chunk_sys.ndim == 1:
                        s = np.column_stack((chunk_sys, chunk_sys))
                    elif chunk_sys.shape[1] == 1:
                        s = np.column_stack((chunk_sys[:, 0], chunk_sys[:, 0]))
                    else:
                        s = chunk_sys[:, :2]
                    n = min(len(m), len(s))
                    m2 = np.column_stack((m[:n], m[:n]))
                    stereo = np.clip(m2 * 0.5 + s[:n] * 0.5, -1.0, 1.0)
                pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16)
                self.wav_file.writeframes(pcm.tobytes())
            except Exception:
                pass

    def pause(self) -> None:
        self.is_paused = True

    def resume(self) -> None:
        self.is_paused = False

    def stop(self) -> None:
        self.is_recording = False
        if self.write_thread:
            self.write_thread.join(timeout=2.0)
        for stream in (self.stream_mic, self.stream_sys):
            if stream is None:
                continue
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        if self.wav_file:
            try:
                self.wav_file.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Screen recorder (ffmpeg pipe = fast stop)
# ---------------------------------------------------------------------------

@dataclass
class RecorderConfig:
    target: dict | None = None
    mic_idx: int | None = None
    sys_idx: int | None = None
    fps: int = 30
    resolution: str = "1080p"  # 1080p | 720p
    highlight_cursor: bool = True
    cursor_color_bgr: tuple[int, int, int] = (0, 255, 255)
    cursor_radius: int = 22
    # Called with BGR preview frame (already resized small)
    preview_cb: Callable[[np.ndarray], None] | None = None
    # Optional: returns RGBA numpy overlay matching capture size
    overlay_provider: Callable[[], np.ndarray | None] | None = None


class ScreenRecorder:
    def __init__(self, **kwargs) -> None:
        # Backward-compatible constructor used by old UI
        if "hwnd" in kwargs and "target" not in kwargs:
            hwnd = int(kwargs.get("hwnd") or 0)
            if hwnd > 0:
                kwargs["target"] = {"hwnd": hwnd, "kind": "window"}
            else:
                mons = get_monitors()
                kwargs["target"] = mons[0] if mons else None
        color = kwargs.pop("highlight_color", None)
        if color and "cursor_color_bgr" not in kwargs:
            # may be RGBA or RGB
            if len(color) >= 3:
                # PIL RGB -> BGR
                kwargs["cursor_color_bgr"] = (int(color[2]), int(color[1]), int(color[0]))
        # draw_overlay widget support
        draw_overlay = kwargs.pop("draw_overlay", None)
        if draw_overlay is not None and "overlay_provider" not in kwargs:

            def _prov():
                pil = getattr(draw_overlay, "overlay_pil", None)
                if pil is None:
                    return None
                try:
                    arr = np.array(pil)
                    if arr.ndim == 3 and arr.shape[2] == 4:
                        return arr
                except Exception:
                    return None
                return None

            kwargs["overlay_provider"] = _prov

        self.cfg = RecorderConfig(
            target=kwargs.get("target"),
            mic_idx=kwargs.get("mic_idx"),
            sys_idx=kwargs.get("sys_idx"),
            # 20fps is more reliable than 30 on heavy desktops (keeps A/V duration aligned)
            fps=int(kwargs.get("fps") or 20),
            resolution=str(kwargs.get("resolution") or "1080p"),
            highlight_cursor=bool(kwargs.get("highlight_cursor", True)),
            cursor_color_bgr=kwargs.get("cursor_color_bgr", (0, 255, 255)),
            cursor_radius=int(kwargs.get("cursor_radius") or 22),
            preview_cb=kwargs.get("preview_cb"),
            overlay_provider=kwargs.get("overlay_provider"),
        )
        res_map = {
            "720p": (1280, 720),
            "1080p": (1920, 1080),
            "1440p": (2560, 1440),
            "2k": (2560, 1440),
            "4k": (3840, 2160),
            "2160p": (3840, 2160),
        }
        self.target_size = res_map.get(str(self.cfg.resolution).lower(), (1920, 1080))

        self.is_recording = False
        self.is_paused = False
        self.duration_seconds = 0.0
        self._t0 = 0.0
        self._pause_acc = 0.0
        self._pause_at = 0.0

        self._work_dir = Path(tempfile.mkdtemp(prefix="qpp_rec_"))
        self.temp_video = str(self._work_dir / "video.mp4")
        self.temp_audio = str(self._work_dir / "audio.wav")

        self.video_thread: threading.Thread | None = None
        self.audio_recorder: AudioRecorder | None = None
        self._ffmpeg: subprocess.Popen | None = None
        self._last_preview_t = 0.0
        self._error = ""

    # --- control ---
    def start(self) -> None:
        self.is_recording = True
        self.is_paused = False
        self.duration_seconds = 0.0
        self._t0 = time.time()
        self._pause_acc = 0.0
        self._error = ""

        if self.cfg.mic_idx is not None or self.cfg.sys_idx is not None:
            self.audio_recorder = AudioRecorder(self.cfg.mic_idx, self.cfg.sys_idx, self.temp_audio)
            self.audio_recorder.start()

        self.video_thread = threading.Thread(target=self._video_loop, daemon=True, name="RecVideo")
        self.video_thread.start()

    def pause(self) -> None:
        if self.is_paused:
            return
        self.is_paused = True
        self._pause_at = time.time()
        if self.audio_recorder:
            self.audio_recorder.pause()

    def resume(self) -> None:
        if not self.is_paused:
            return
        self.is_paused = False
        self._pause_acc += time.time() - self._pause_at
        if self.audio_recorder:
            self.audio_recorder.resume()

    def halt_capture(self) -> None:
        """Stop capturing immediately; keep temp files for a later export()."""
        self.is_recording = False
        if self.video_thread:
            self.video_thread.join(timeout=8.0)
            self.video_thread = None
        if self.audio_recorder:
            try:
                self.audio_recorder.stop()
            except Exception:
                pass
        if self._ffmpeg:
            try:
                if self._ffmpeg.stdin:
                    self._ffmpeg.stdin.close()
            except Exception:
                pass
            try:
                self._ffmpeg.wait(timeout=15)
            except Exception:
                try:
                    self._ffmpeg.kill()
                except Exception:
                    pass
            self._ffmpeg = None

    def export(self, final_output_path: str) -> str:
        """Mux halted capture to final path (stream-copy video — no re-encode)."""
        out = Path(final_output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        has_audio = os.path.exists(self.temp_audio) and os.path.getsize(self.temp_audio) > 44
        has_video = os.path.exists(self.temp_video) and os.path.getsize(self.temp_video) > 1000
        wanted_audio = self.cfg.mic_idx is not None or self.cfg.sys_idx is not None

        if not has_video:
            self._cleanup_temp()
            return self._error or "录制失败：没有生成视频帧。"

        ok = fast_mux(self.temp_video, self.temp_audio if has_audio else None, str(out))
        self._cleanup_temp()
        if ok and out.exists():
            # Confirm audio track actually landed in the file
            has_a = False
            try:
                r = subprocess.run(
                    [_ffmpeg_bin(), "-i", str(out)],
                    capture_output=True,
                    timeout=20,
                    **_silent_subprocess_kwargs(),
                )
                text = (r.stderr or b"").decode("utf-8", errors="replace")
                has_a = "Audio:" in text
            except Exception:
                has_a = has_audio
            if wanted_audio and not has_audio:
                return f"已保存（无声音：麦克风/系统声未能采集到）：{out}"
            if has_audio and not has_a:
                return f"已保存（画面成功，音轨封装失败）：{out}"
            if has_a:
                return f"已保存（含声音）：{out}"
            return f"已保存：{out}"
        return f"保存失败（{self._error or 'mux error'}）"

    def discard(self) -> str:
        """Drop temp capture without exporting."""
        self._cleanup_temp()
        return "已取消保存"

    def stop(self, final_output_path: str) -> str:
        """Stop capture and mux. Returns status message. Fast path: stream-copy video."""
        self.halt_capture()
        return self.export(final_output_path)

    def _cleanup_temp(self) -> None:
        try:
            shutil.rmtree(self._work_dir, ignore_errors=True)
        except Exception:
            pass

    def _update_duration(self) -> None:
        if not self.is_recording:
            return
        if self.is_paused:
            self.duration_seconds = max(0.0, self._pause_at - self._t0 - self._pause_acc)
        else:
            self.duration_seconds = max(0.0, time.time() - self._t0 - self._pause_acc)

    def _start_ffmpeg(self, w: int, h: int) -> bool:
        ffmpeg = _ffmpeg_bin()
        # ultrafast + yuv420p for quick encode and wide player compatibility
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "rawvideo",
            "-vcodec",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{w}x{h}",
            "-r",
            str(self.cfg.fps),
            "-i",
            "-",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            self.temp_video,
        ]
        try:
            self._ffmpeg = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_silent_subprocess_kwargs(),
            )
            return True
        except Exception as exc:
            self._error = f"ffmpeg 启动失败: {exc}"
            print(self._error, flush=True)
            return False

    def _video_loop(self) -> None:
        tw, th = self.target_size
        # ensure even dimensions for yuv420p
        tw -= tw % 2
        th -= th % 2
        self.target_size = (tw, th)

        use_ffmpeg = self._start_ffmpeg(tw, th)
        writer = None
        if not use_ffmpeg:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(self.temp_video, fourcc, self.cfg.fps, (tw, th))
            if not writer.isOpened():
                self._error = "无法打开视频写入器"
                self.is_recording = False
                return

        frame_delay = 1.0 / max(1, self.cfg.fps)
        frames = 0
        last_frame = None
        blank_streak = 0
        # Absolute pacing: keep video wall-clock duration ≈ audio duration.
        # If capture is slow, duplicate the last frame so muxed video isn't "sped up".
        next_t = time.perf_counter()
        while self.is_recording:
            self._update_duration()
            if self.is_paused:
                time.sleep(0.05)
                # Freeze schedule while paused so resume stays in sync
                next_t = time.perf_counter()
                continue

            now = time.perf_counter()
            if now < next_t:
                time.sleep(min(0.05, next_t - now))
                continue

            region = resolve_region(self.cfg.target)
            frame = capture_bgr(region)
            if frame is None or _bgr_is_blank(frame):
                blank_streak += 1
                # Do not keep writing white/black frames (looks like "exported white video")
                if last_frame is not None and blank_streak < self.cfg.fps * 2:
                    frame = last_frame
                elif blank_streak >= max(8, int(self.cfg.fps)):
                    if sys.platform == "darwin":
                        self._error = (
                            "录制中断：屏幕画面为空。请在「系统设置 → 隐私与安全 → 屏幕录制」"
                            "允许本应用后，完全退出再重试。"
                        )
                    else:
                        self._error = "录制中断：无法捕获屏幕画面。"
                    break
                else:
                    next_t += frame_delay
                    time.sleep(0.01)
                    continue
            else:
                blank_streak = 0

            # Overlay annotations
            if self.cfg.overlay_provider:
                try:
                    ov = self.cfg.overlay_provider()
                    frame = blend_overlay_rgba(frame, ov)
                except Exception:
                    pass

            if self.cfg.highlight_cursor:
                draw_cursor_highlight(
                    frame,
                    region,
                    color_bgr=self.cfg.cursor_color_bgr,
                    radius=self.cfg.cursor_radius,
                )

            if frame.shape[1] != tw or frame.shape[0] != th:
                frame = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)

            last_frame = frame
            # Catch up: write this frame once, then pad duplicates if we're late
            catchup = 0
            while True:
                try:
                    if self._ffmpeg and self._ffmpeg.stdin:
                        self._ffmpeg.stdin.write(frame.tobytes())
                    elif writer is not None:
                        writer.write(frame)
                    frames += 1
                except Exception as exc:
                    self._error = f"写帧失败: {exc}"
                    break
                next_t += frame_delay
                catchup += 1
                # At most ~0.5s of duplicates per capture (avoid spiral)
                if catchup >= max(1, int(self.cfg.fps // 2)):
                    next_t = time.perf_counter()
                    break
                if next_t >= time.perf_counter() - frame_delay * 0.25:
                    break
                # Keep padding with same frame so timeline matches wall clock
            if self._error:
                break

            # Preview ~4 fps (enough for HUD, less UI contention while recording)
            if self.cfg.preview_cb and (time.time() - self._last_preview_t) > 0.25:
                self._last_preview_t = time.time()
                try:
                    small = cv2.resize(frame, (280, 158), interpolation=cv2.INTER_AREA)
                    self.cfg.preview_cb(small)
                except Exception:
                    pass

        if writer is not None:
            writer.release()
        if self._ffmpeg and self._ffmpeg.stdin:
            try:
                self._ffmpeg.stdin.close()
            except Exception:
                pass
            try:
                self._ffmpeg.wait(timeout=20)
            except Exception:
                try:
                    self._ffmpeg.kill()
                except Exception:
                    pass
            self._ffmpeg = None
        if frames == 0 and not self._error:
            self._error = "未捕获到任何画面"


def _probe_duration_seconds(path: str) -> float | None:
    """Best-effort media duration via ffmpeg -i stderr (no ffprobe required)."""
    try:
        r = subprocess.run(
            [_ffmpeg_bin(), "-i", path],
            capture_output=True,
            timeout=20,
            **_silent_subprocess_kwargs(),
        )
        text = (r.stderr or b"").decode("utf-8", errors="replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if not m:
            return None
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + mi * 60 + s
    except Exception:
        return None


def _run_ffmpeg(cmd: list[str], *, timeout: int = 60) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            **_silent_subprocess_kwargs(),
        )
        err = (r.stderr or b"").decode("utf-8", errors="replace")[-800:]
        ok = r.returncode == 0 and os.path.exists(cmd[-1])
        return ok, err
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out"
    except Exception as exc:
        return False, str(exc)


def fast_mux(video_path: str, audio_path: str | None, output_path: str) -> bool:
    """Mux with stream-copy video. Avoid infinite apad (hangs + silent audio drop)."""
    ffmpeg = _ffmpeg_bin()
    try:
        has_audio = bool(
            audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 44
        )
        if not has_audio:
            ok, _ = _run_ffmpeg(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    video_path,
                    "-c:v",
                    "copy",
                    "-an",
                    "-movflags",
                    "+faststart",
                    output_path,
                ]
            )
            if ok:
                return True
            shutil.copy2(video_path, output_path)
            return os.path.exists(output_path)

        # 1) Prefer finite pad to video length (keeps A/V length close without hanging).
        #    Bare `-af apad` pads forever and with `-c:v copy` often never ends → timeout
        #    then old code fell back to video-only (有画面没声音).
        vdur = _probe_duration_seconds(video_path)
        attempts: list[list[str]] = []
        if vdur and vdur > 0.05:
            attempts.append(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    video_path,
                    "-i",
                    str(audio_path),
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-af",
                    f"apad=whole_dur={vdur:.3f}",
                    "-t",
                    f"{vdur:.3f}",
                    "-movflags",
                    "+faststart",
                    output_path,
                ]
            )
        # 2) Simple shortest mux (no pad) — reliable and fast
        attempts.append(
            [
                ffmpeg,
                "-y",
                "-i",
                video_path,
                "-i",
                str(audio_path),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                output_path,
            ]
        )
        # 3) Last resort: re-encode video too (still keep audio)
        attempts.append(
            [
                ffmpeg,
                "-y",
                "-i",
                video_path,
                "-i",
                str(audio_path),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                output_path,
            ]
        )

        last_err = ""
        for cmd in attempts:
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
            ok, last_err = _run_ffmpeg(cmd, timeout=90)
            if ok:
                # Sanity: refuse "success" that dropped the audio track
                probe = subprocess.run(
                    [ffmpeg, "-i", output_path],
                    capture_output=True,
                    timeout=20,
                    **_silent_subprocess_kwargs(),
                )
                text = (probe.stderr or b"").decode("utf-8", errors="replace")
                if "Audio:" in text:
                    return True
                last_err = "mux output missing audio stream"
                print(f"mux missing audio, retry: {last_err}", flush=True)

        print(f"mux failed (keeping video only as last resort): {last_err}", flush=True)
        try:
            shutil.copy2(video_path, output_path)
            return os.path.exists(output_path)
        except Exception:
            return False
    except Exception as exc:
        print(f"mux failed: {exc}", flush=True)
        try:
            shutil.copy2(video_path, output_path)
            return True
        except Exception:
            return False


# Keep old name used elsewhere
merge_audio_video = fast_mux
