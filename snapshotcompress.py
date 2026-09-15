#!/usr/bin/env python3
# =============================================================================
# 🚢 MARITIME CCTV SNAPSHOT & MOTION DETECTION AGENT (DUAL-LANE ARCHITECTURE)
# =============================================================================
# 1. Jalur API 1: Snapshot Rutin (Per-Menit)     -> POST /cctv/worker/cameras/{token}/snapshots
# 2. Jalur API 2: Motion Video Alert (5 Menit)   -> POST /cctv/worker/cameras/{token}/motions
#    (Dengan Real-time Snapshot kejadian otomatis dipasangkan sebagai Thumbnail Cover WebP)
#
# Fitur Utama:
# - Multi-Brand Driver: Hikvision (ISAPI), Dahua (CGI), ONVIF (PullPoint), Fallback RTSP
# - Anti-Overlapping Event Windowing: max(Alert - 3s, Last Recorded End)
# - Hardware Acceleration Intel VA-API (/dev/dri/renderD128) + Fallback CPU libx264
# - Auto-Purge HTTP 400/401/403/404/422 (Anti-Deadlock)
# - Shared Connection Flag: Offline safe, queue di SQLite WAL (queue.db)
# =============================================================================

import os
import sys
import time
import json
import sqlite3
import subprocess
import threading
import shutil
import re
from datetime import datetime, timezone, timedelta
import cv2
import numpy as np
import requests
from requests.auth import HTTPDigestAuth, HTTPBasicAuth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Optimasi Driver Intel GPU (CherryView / Braswell N3160)
os.environ.setdefault("LIBVA_DRIVER_NAME", "i965")

# =============================================================================
# LOGGING HARIAN (DAILY ROTATED LOGGER)
# =============================================================================
class DailyRotatedLogger:
    def __init__(self, log_dir):
        self.log_dir = log_dir
        self.current_date = None
        self.file_handle = None
        self.lock = threading.Lock()
        self.terminal = sys.stdout
        os.makedirs(self.log_dir, exist_ok=True)

    def _get_log_file(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.current_date or self.file_handle is None:
            if self.file_handle:
                try:
                    self.file_handle.close()
                except Exception:
                    pass
            self.current_date = today
            log_path = os.path.join(self.log_dir, f"agent_{today}.log")
            self.file_handle = open(log_path, "a", encoding="utf-8", buffering=1)
        return self.file_handle

    def write(self, message):
        self.terminal.write(message)
        with self.lock:
            try:
                f = self._get_log_file()
                f.write(message)
            except Exception:
                pass

    def flush(self):
        self.terminal.flush()
        with self.lock:
            if self.file_handle:
                try:
                    self.file_handle.flush()
                except Exception:
                    pass

LOGS_DIR = os.path.join(BASE_DIR, "logs")
daily_logger = DailyRotatedLogger(LOGS_DIR)
sys.stdout = daily_logger
sys.stderr = daily_logger

# =============================================================================
# LOAD CONFIGURATION (config.json)
# =============================================================================
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
if not os.path.exists(CONFIG_PATH):
    print(f"[FATAL] File konfigurasi tidak ditemukan: {CONFIG_PATH}")
    sys.exit(1)

with open(CONFIG_PATH, "r") as f:
    CFG = json.load(f)

NVR_IP                      = CFG["nvr"]["ip"]
NVR_PORT                    = CFG["nvr"].get("port", 554)
NVR_HTTP_PORT               = CFG["nvr"].get("http_port", 80)
NVR_USER                    = CFG["nvr"]["user"]
NVR_PASS                    = CFG["nvr"]["pass"]
NVR_BRAND                   = CFG["nvr"].get("brand", "hikvision").lower()

SERVER_BASE_URL             = CFG["server"]["base_url"]
SNAPSHOT_ENDPOINT_TMPL      = CFG["server"].get("snapshot_endpoint", "/cctv/worker/cameras/{cameraToken}/snapshots")
MOTION_VIDEO_ENDPOINT_TMPL  = CFG["server"].get("motion_video_endpoint", "/cctv/worker/cameras/{cameraToken}/motions")

SNAPSHOT_INTERVAL_SEC       = CFG["agent"].get("snapshot_interval_sec", 60)
CONNECTION_CHECK_INTERVAL   = CFG["agent"].get("connection_check_interval_sec", 60)
HD_RETENTION_DAYS           = CFG["agent"].get("hd_retention_days", 30)

MOTION_WINDOW_SEC           = CFG["agent"].get("motion_window_sec", 300)       # 5 Menit Windowing
MOTION_CLIP_DURATION_SEC    = CFG["agent"].get("motion_clip_duration_sec", 10) # 10 Detik per klip
MOTION_PRE_EVENT_SEC        = CFG["agent"].get("motion_pre_event_sec", 3)      # 3 Detik before-event
MOTION_COOLDOWN_SEC         = CFG["agent"].get("motion_cooldown_sec", 0)
MOTION_CAPTURE_DELAY_SEC    = float(CFG["agent"].get("motion_capture_delay_sec", 0.0))

VAAPI_DEVICE                = CFG["agent"].get("vaapi_device", "/dev/dri/renderD128")
VIDEO_SCALE_W               = CFG["agent"].get("video_scale_w", 640)
VIDEO_FPS                   = CFG["agent"].get("video_fps", 15)

MOCK_MODE                   = CFG["agent"].get("mock_mode", False)
MOCK_VIDEO                  = CFG["agent"].get("mock_video", "cctv.kapal.mp4")

CAMERAS                     = CFG["cameras"]

# Saklar Koneksi Server Darat & Lock Thread-Safe Database
server_connected = threading.Event()
db_lock          = threading.Lock()

# Kompresi WebP (< 10.0 KB Guarantee)
SNAPSHOT_SCALE    = '360:270'   # Resolusi piksel statik 360 x 270
INITIAL_QUALITY   = 8           # Quality WebP awal (0-100)
COMPRESSION_LEVEL = 6           # Kompresi libwebp maksimal (0-6)
MAX_TARGET_KB     = 10.0        # Batas maksimal ukuran file WebP (< 10.0 KB)

OUTPUT_HD_DIR     = os.path.join(BASE_DIR, "snapshots_hd_lokal")  # Folder HD Asli (Bukti Lokal Rutin)
OUTPUT_DIR        = os.path.join(BASE_DIR, "snapshots_nvr_4cctv") # Folder WebP Ringan (< 10 KB)
TEMP_CLIPS_DIR    = os.path.join(BASE_DIR, "temp_clips")          # Folder Klip Sementara 10 Detik
MOTION_VIDEOS_DIR = os.path.join(BASE_DIR, "motion_videos")       # Folder Video 360p Siap Upload
DB_PATH           = os.path.join(BASE_DIR, "queue.db")            # Database Antrean Offline SQLite
FFMPEG_CMD        = "ffmpeg"

def init_db():
    """Inisialisasi SQLite database dengan WAL mode untuk ketahanan daya di kapal."""
    os.makedirs(OUTPUT_HD_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(TEMP_CLIPS_DIR, exist_ok=True)
    os.makedirs(MOTION_VIDEOS_DIR, exist_ok=True)

    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.execute('''
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_token TEXT NOT NULL,
                camera_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                is_uploading INTEGER DEFAULT 0,
                event_type TEXT DEFAULT 'snapshot',
                duration_sec REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Reset state zombie uploads saat agent restart
        c.execute("UPDATE queue SET is_uploading = 0 WHERE is_uploading != 0")
        conn.commit()
        conn.close()

def cleanup_old_hd_snapshots():
    """Membersihkan folder snapshot HD lokal yang umurnya melebihi batas retensi."""
    if not os.path.exists(OUTPUT_HD_DIR):
        return

    now = datetime.now()
    retention_cutoff = now - timedelta(days=HD_RETENTION_DAYS)
    cutoff_folder_name = retention_cutoff.strftime("%Y-%m-%d")

    try:
        subfolders = [f for f in os.listdir(OUTPUT_HD_DIR) if os.path.isdir(os.path.join(OUTPUT_HD_DIR, f))]
        for folder in subfolders:
            if re.match(r"^\d{4}-\d{2}-\d{2}$", folder):
                if folder < cutoff_folder_name:
                    full_path = os.path.join(OUTPUT_HD_DIR, folder)
                    print(f"[{datetime.now()}] [Auto-Purge HD] Menghapus arsip lama: {folder} (>{HD_RETENTION_DAYS} hari)")
                    subprocess.run(["rm", "-rf", full_path], check=False)
    except Exception as e:
        print(f"[Auto-Purge HD Error] Gagal membersihkan folder: {e}")

def build_rtsp_url(channel: int) -> str:
    """Membangun RTSP URL CCTV NVR berdasarkan channel."""
    return f"rtsp://{NVR_USER}:{NVR_PASS}@{NVR_IP}:{NVR_PORT}/Streaming/Channels/{channel}01"

# =============================================================================
# MULTI-BRAND FAST SNAPSHOT ENGINE (DENGAN 4 TINGKAT FALLBACK)
# =============================================================================
def capture_raw_hd_snapshot(cam_info: dict, hd_filepath: str, is_motion_event: bool = False) -> tuple:
    """
    Mengambil snapshot resolusi HD dengan 4 lapis hierarki:
    Langkah 1: Coba Hikvision ISAPI /picture HTTP GET instan (~0.2 detik).
    Langkah 2: Coba Dahua CGI snapshot.cgi (~0.2 detik).
    Langkah 3: Coba ONVIF GetSnapshotURI.
    Langkah 4: Fallback terakhir ke FFMPEG RTSP Snapshot (Dijamin pasti tembus).
    Mengembalikan (success: bool, tier_name: str, elapsed_sec: float).
    """
    channel_num = cam_info["channel"]
    source_input = os.path.join(BASE_DIR, MOCK_VIDEO) if MOCK_MODE else build_rtsp_url(channel_num)
    t_start = time.time()

    # 1. Mode Mock Simulation
    if MOCK_MODE:
        mock_sec = (channel_num * 5 + int(time.time() % 20)) % 25
        hd_cmd = [FFMPEG_CMD, "-y", "-ss", f"00:00:{mock_sec:02d}", "-i", source_input, "-vframes", "1", "-q:v", "2", hd_filepath]
        try:
            subprocess.run(hd_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            elapsed = time.time() - t_start
            return (os.path.exists(hd_filepath), "Tier Mock (Video Simulasi MP4)", elapsed)
        except Exception:
            return (False, "Tier Mock (Gagal)", time.time() - t_start)

    # 2. Langkah 1: Hikvision ISAPI HTTP Picture (Instan ~0.2 detik)
    if NVR_BRAND == "hikvision":
        url = f"http://{NVR_IP}:{NVR_HTTP_PORT}/ISAPI/Streaming/channels/{channel_num}01/picture"
        for auth_cls in [HTTPDigestAuth, HTTPBasicAuth]:
            try:
                res = requests.get(url, auth=auth_cls(NVR_USER, NVR_PASS), timeout=2.5)
                if res.status_code == 200 and len(res.content) > 1000:
                    with open(hd_filepath, "wb") as f:
                        f.write(res.content)
                    elapsed = time.time() - t_start
                    return (True, "Tier 1: Hikvision ISAPI HTTP /picture (Native API)", elapsed)
            except Exception:
                pass

    # 3. Langkah 2: Dahua CGI Snapshot
    elif NVR_BRAND == "dahua":
        url = f"http://{NVR_IP}:{NVR_HTTP_PORT}/cgi-bin/snapshot.cgi?channel={channel_num}"
        for auth_cls in [HTTPDigestAuth, HTTPBasicAuth]:
            try:
                res = requests.get(url, auth=auth_cls(NVR_USER, NVR_PASS), timeout=2.5)
                if res.status_code == 200 and len(res.content) > 1000:
                    with open(hd_filepath, "wb") as f:
                        f.write(res.content)
                    elapsed = time.time() - t_start
                    return (True, "Tier 2: Dahua CGI /snapshot.cgi (Native API)", elapsed)
            except Exception:
                pass

    # 4. Langkah 3: ONVIF Snapshot (Jika diimplementasikan lebih lanjut)
    # (Jika ada library onvif-zeep terkonfigurasi)

    # 5. Langkah 4: FALLBACK TERAKHIR KE FFMPEG RTSP SNAPSHOT
    # Menjamin kamera apa pun selalu bisa di-capture frame-nya
    hd_cmd = [
        FFMPEG_CMD, "-y",
        "-rtsp_transport", "tcp",
        "-timeout", "5000000",
        "-i", source_input,
        "-vf", r"select=eq(pict_type\,I)",  # Filter I-Frame HEVC/H.264
        "-vframes", "1",
        "-q:v", "2",
        hd_filepath
    ]
    try:
        subprocess.run(hd_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        elapsed = time.time() - t_start
        return (os.path.exists(hd_filepath), "Tier 4: Fallback FFMPEG RTSP Stream (Universal)", elapsed)
    except Exception as e:
        elapsed = time.time() - t_start
        print(f"[Snapshot Error Ch {channel_num}] Fallback RTSP gagal: {e}")
        return (False, f"Tier 4: Gagal RTSP ({e})", elapsed)

# =============================================================================
# ENGINE KOMPRESI WEBP (< 10 KB) & PIPELINE DATABASE
# =============================================================================
def take_nvr_snapshot(cam_info: dict, is_motion_event: bool = False, event_type: str = "snapshot", save_to_queue: bool = True) -> dict:
    """
    Mengambil snapshot dan mengompres ke WebP <10KB:
    1. Ambil HD Asli (.jpg) dengan log tingkat Tier yang berhasil
    2. Kompres ke WebP Adaptif (Statik 360x270, cwebp -size 9500) dengan log rasio penghematan
    3. Jika save_to_queue=True: Simpan ke antrean queue.db untuk upload rutin
       Jika save_to_queue=False: Disimpan sebagai thumbnail video motion alert
    """
    channel_num = cam_info["channel"]
    camera_token = cam_info["token"]

    now_dt = datetime.now()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    date_folder = now_dt.strftime("%Y-%m-%d")

    unix_ms = str(int(time.time() * 1000))
    suffix = "_motion" if is_motion_event else ""
    file_base = f"{unix_ms}_ch{channel_num:02d}{suffix}"

    hd_date_dir = os.path.join(OUTPUT_HD_DIR, date_folder)
    os.makedirs(hd_date_dir, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    hd_filename = f"{file_base}_hd.jpg"
    webp_filename = f"{file_base}.webp"

    hd_filepath = os.path.join(hd_date_dir, hd_filename)
    webp_filepath = os.path.join(OUTPUT_DIR, webp_filename)

    # 1. Capture Raw HD Frame via Multi-Brand Engine
    success, tier_used, cap_dur = capture_raw_hd_snapshot(cam_info, hd_filepath, is_motion_event=is_motion_event)
    if not success:
        return {"success": False, "channel": channel_num, "error": f"Gagal capture ({tier_used})"}

    hd_size_kb = os.path.getsize(hd_filepath) / 1024.0
    tag = "🖼️ [MOTION THUMBNAIL]" if is_motion_event else "📸 [ROUTINE SNAPSHOT]"
    print(f"   {tag} Ch {channel_num} ({cam_info['name']}) -> {tier_used} (Durasi: {cap_dur:.2f}s | Berkas HD: {hd_size_kb:.1f} KB)")

    # 2. Kompresi ke WebP Adaptif (< 10 KB, Statik 360x270)
    TARGET_W, TARGET_H = 360, 270
    MAX_BYTES = 10240
    SAFE_TARGET_BYTES = 9500

    def preprocess_image(img_bgr: np.ndarray, level: int) -> np.ndarray:
        if level == 0:
            return img_bgr
        elif level == 1:
            return cv2.bilateralFilter(img_bgr, d=5, sigmaColor=35, sigmaSpace=35)
        elif level == 2:
            denoised = cv2.fastNlMeansDenoisingColored(img_bgr, None, h=6, hColor=6, templateWindowSize=7, searchWindowSize=21)
            return cv2.GaussianBlur(denoised, (3, 3), 0.5)
        elif level == 3:
            denoised = cv2.fastNlMeansDenoisingColored(img_bgr, None, h=10, hColor=10, templateWindowSize=7, searchWindowSize=21)
            down = cv2.resize(denoised, (int(TARGET_W * 0.8), int(TARGET_H * 0.8)), interpolation=cv2.INTER_AREA)
            return cv2.resize(down, (TARGET_W, TARGET_H), interpolation=cv2.INTER_LINEAR)
        else:
            denoised = cv2.fastNlMeansDenoisingColored(img_bgr, None, h=14, hColor=14, templateWindowSize=7, searchWindowSize=21)
            down = cv2.resize(denoised, (int(TARGET_W * 0.65), int(TARGET_H * 0.65)), interpolation=cv2.INTER_AREA)
            return cv2.resize(down, (TARGET_W, TARGET_H), interpolation=cv2.INTER_LINEAR)

    img_raw = cv2.imread(hd_filepath)
    if img_raw is None:
        return {"success": False, "channel": channel_num, "error": "Gagal membaca berkas HD"}

    orig_h, orig_w = img_raw.shape[:2]
    img_resized = cv2.resize(img_raw, (TARGET_W, TARGET_H), interpolation=cv2.INTER_AREA)
    temp_prep_path = os.path.join(OUTPUT_DIR, f"temp_{file_base}.png")

    compressed_success = False
    final_webp_size_kb = 0.0
    used_prep_level = 0

    for prep_lvl in range(5):
        used_prep_level = prep_lvl
        prep_img = preprocess_image(img_resized, prep_lvl)
        cv2.imwrite(temp_prep_path, prep_img)

        target_bytes = SAFE_TARGET_BYTES if prep_lvl < 3 else (SAFE_TARGET_BYTES - (prep_lvl * 500))
        cwebp_cmd = ["cwebp", "-quiet", "-size", str(target_bytes), "-m", "6", "-mt", temp_prep_path, "-o", webp_filepath]
        try:
            subprocess.run(cwebp_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            if os.path.exists(webp_filepath):
                actual_bytes = os.path.getsize(webp_filepath)
                if actual_bytes <= MAX_BYTES:
                    final_webp_size_kb = actual_bytes / 1024.0
                    compressed_success = True
                    break
        except Exception:
            pass

    if os.path.exists(temp_prep_path):
        try:
            os.remove(temp_prep_path)
        except Exception:
            pass

    # Fallback OpenCV WebP
    if not compressed_success or not os.path.exists(webp_filepath) or os.path.getsize(webp_filepath) > MAX_BYTES:
        for q in range(15, 1, -2):
            cv2.imwrite(webp_filepath, img_resized, [cv2.IMWRITE_WEBP_QUALITY, q])
            actual_bytes = os.path.getsize(webp_filepath)
            if actual_bytes <= MAX_BYTES:
                final_webp_size_kb = actual_bytes / 1024.0
                compressed_success = True
                break

    if not compressed_success:
        return {"success": False, "channel": channel_num, "error": "Gagal kompresi <10KB"}

    savings = max(0, (1 - (final_webp_size_kb / hd_size_kb)) * 100) if hd_size_kb > 0 else 0
    print(f"   🗜️ [WebP Kompresi Ch {channel_num}] Resolusi: {orig_w}x{orig_h} -> {TARGET_W}x{TARGET_H} | Ukuran: {hd_size_kb:.1f} KB -> {final_webp_size_kb:.2f} KB (Hemat: {savings:.1f}%) [Level {used_prep_level}, Target <10KB OK]")

    # 3. Simpan ke antrean SQLite jika save_to_queue aktif
    if save_to_queue:
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''
                INSERT INTO queue (camera_token, camera_name, file_path, captured_at, retry_count, is_uploading, event_type)
                VALUES (?, ?, ?, ?, 0, 0, ?)
            ''', (camera_token, cam_info["name"], webp_filepath, now_str, event_type))
            conn.commit()
            conn.close()
    else:
        print(f"   ✓ [Motion Cover] Snapshot realtime disimpan sebagai thumbnail video alert: {webp_filename}")

    return {
        "success": True,
        "channel": channel_num,
        "hd_path": hd_filepath,
        "hd_size_kb": hd_size_kb,
        "webp_path": webp_filepath,
        "webp_size_kb": final_webp_size_kb,
        "event_type": event_type,
        "tier_used": tier_used
    }

# =============================================================================
# ENGINE KOMPRESI & TRANSCODE VIDEO (INTEL VA-API + FALLBACK CPU)
# =============================================================================
def transcode_video_to_360p(input_path: str, output_path: str) -> bool:
    """
    Menurunkan resolusi video ke 360p (640x360) sesuai arahan atasan:
    - Hanya merubah resolusi ke 360p, tidak mengompres/merusak visual secara agresif.
    - Menggunakan Intel VA-API (/dev/dri/renderD128) jika tersedia.
    - Fallback otomatis ke FFMPEG CPU (libx264 -preset veryfast) jika VA-API belum aktif.
    """
    in_size_kb = os.path.getsize(input_path) / 1024.0
    cap = cv2.VideoCapture(input_path)
    in_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    in_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    in_fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()

    print(f"   🔄 [Transcode Engine] Menurunkan resolusi video...")
    print(f"      • Input Video  : {in_w}x{in_h} @ {in_fps:.0f}fps | Ukuran: {in_size_kb:.1f} KB")
    res_label = f"W{VIDEO_SCALE_W}" if VIDEO_SCALE_W != 640 else "360p"
    print(f"      • Target Format: Lebar {VIDEO_SCALE_W}px ({res_label}) @ {VIDEO_FPS}fps (Visual Jernih, Standar Normal)")

    # 1. Coba Hardware Acceleration Intel VA-API
    if os.path.exists(VAAPI_DEVICE):
        scale_h = int(in_h * (VIDEO_SCALE_W / in_w)) & ~1 if (in_w > 0 and in_h > 0) else 360
        vaapi_env = os.environ.copy()
        vaapi_env["LIBVA_DRIVER_NAME"] = "i965"
        vaapi_cmd = [
            FFMPEG_CMD, "-y",
            "-vaapi_device", VAAPI_DEVICE,
            "-i", input_path,
            "-vf", f"format=nv12,hwupload,scale_vaapi=w={VIDEO_SCALE_W}:h={scale_h}",
            "-c:v", "h264_vaapi",
            "-qp", "24",
            "-r", str(VIDEO_FPS),
            "-an",
            "-movflags", "+faststart",
            output_path
        ]
        try:
            res = subprocess.run(vaapi_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=vaapi_env)
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                out_size_kb = os.path.getsize(output_path) / 1024.0
                savings = max(0, (1 - (out_size_kb / in_size_kb)) * 100) if in_size_kb > 0 else 0
                print(f"      • Engine       : 🚀 Hardware Acceleration Intel VA-API ({VAAPI_DEVICE})")
                print(f"      • Hasil Video  : {in_size_kb:.1f} KB -> {out_size_kb:.1f} KB (Hemat: {savings:.1f}%) | Resolusi: {res_label} | Faststart: ON")
                return True
        except Exception:
            pass

    # 2. Fallback Universal CPU (libx264 - Kualitas Jernih Tanpa Rusak)
    cpu_cmd = [
        FFMPEG_CMD, "-y",
        "-i", input_path,
        "-vf", f"scale={VIDEO_SCALE_W}:-2",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",            # Standar kualitas visual tajam dan jernih
        "-r", str(VIDEO_FPS),
        "-an",
        "-movflags", "+faststart",
        output_path
    ]
    try:
        res = subprocess.run(cpu_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            out_size_kb = os.path.getsize(output_path) / 1024.0
            savings = max(0, (1 - (out_size_kb / in_size_kb)) * 100) if in_size_kb > 0 else 0
            print(f"      • Engine       : 💻 CPU Software FFMPEG (libx264 -preset veryfast -crf 23)")
            print(f"      • Hasil Video  : {in_size_kb:.1f} KB -> {out_size_kb:.1f} KB (Hemat: {savings:.1f}%) | Resolusi: {res_label} | Faststart: ON")
            return True
        return False
    except Exception as e:
        print(f"[Transcode Error] Gagal transcode CPU: {e}")
        return False

# =============================================================================
# MOTION WINDOW MANAGER (5-MINUTE WINDOWING & ANTI-OVERLAPPING BOUNDARY)
# =============================================================================
class MotionWindowManager:
    """
    Mengelola siklus window 5 menit per kamera:
    - Menghitung batas start klip dengan rumus non-overlapping: max(Alert - 3s, Last Recorded End)
    - Memotong klip 10 detik dari rekaman NVR
    - Pada menit ke-5, menggabungkan semua klip gerakan dan men-transcode ke 360p
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.channels = {}  # channel_num -> {state, window_start, clips, last_recorded_end, is_busy}

    def _get_channel_state(self, channel_num: int):
        if channel_num not in self.channels:
            self.channels[channel_num] = {
                "state": "IDLE",            # 'IDLE' atau 'COLLECTING'
                "window_start_time": 0.0,
                "clips": [],                # list of temp clip filepaths
                "last_recorded_end_dt": None, # datetime NVR end time
                "is_busy_recording": False,
                "thumbnail_path": None      # Realtime WebP snapshot sebagai cover video
            }
        return self.channels[channel_num]

    def should_capture_thumbnail(self, channel_num: int) -> bool:
        """Memeriksa apakah jendela kamera ini belum memiliki thumbnail cover (hanya 1x per window)."""
        with self.lock:
            cstate = self._get_channel_state(channel_num)
            return cstate["thumbnail_path"] is None

    def on_motion_event(self, cam_info: dict, nvr_datetime_str: str = None, thumbnail_path: str = None):
        """Dipanggil saat ada event gerakan terverifikasi dari NVR."""
        channel_num = cam_info["channel"]
        with self.lock:
            cstate = self._get_channel_state(channel_num)

            # Simpan snapshot gerakan pertama pada window ini sebagai cover thumbnail
            if cstate["thumbnail_path"] is None and thumbnail_path and os.path.exists(thumbnail_path):
                cstate["thumbnail_path"] = thumbnail_path

            # Jika sedang memotong klip sebelumnya, abaikan spam alert XML
            if cstate["is_busy_recording"]:
                return

            cstate["is_busy_recording"] = True
            now_ts = time.time()

            # Buka window 5 menit jika saat ini IDLE
            if cstate["state"] == "IDLE":
                cstate["state"] = "COLLECTING"
                cstate["window_start_time"] = now_ts
                cstate["clips"] = []
                win_str = f"{MOTION_WINDOW_SEC // 60} Menit" if MOTION_WINDOW_SEC % 60 == 0 else f"{MOTION_WINDOW_SEC} Detik"
                print(f"\n⏱️ [MotionWindow Ch {channel_num}] 🌟 Membuka Jendela {win_str} (Timer {MOTION_WINDOW_SEC}s dimulai)...")

        # Jalankan pemotongan klip di thread terpisah agar tidak mem-block event listener
        threading.Thread(
            target=self._cut_clip_worker,
            args=(cam_info, nvr_datetime_str),
            daemon=True
        ).start()

    def _cut_clip_worker(self, cam_info: dict, nvr_datetime_str: str = None):
        """Memotong klip 10 detik dari rekaman NVR berdasarkan jam NVR."""
        channel_num = cam_info["channel"]
        try:
            # Parse datetime NVR dari XML (atau fallback ke waktu lokal)
            dt_nvr = None
            if nvr_datetime_str:
                try:
                    # Format umum: 2026-09-07T17:10:00+07:00 atau 2026-09-07T17:10:00Z
                    clean_str = re.sub(r'([+-]\d{2}):(\d{2})$', r'\1\2', nvr_datetime_str)
                    dt_nvr = datetime.fromisoformat(clean_str)
                except Exception:
                    pass

            if dt_nvr is None:
                dt_nvr = datetime.now()

            with self.lock:
                cstate = self._get_channel_state(channel_num)
                last_end = cstate["last_recorded_end_dt"]

                # RUMUS ANTI-OVERLAPPING BOUNDARY:
                # Start = max(NVR_Alert - 3s, Last_Recorded_End)
                target_start = dt_nvr - timedelta(seconds=MOTION_PRE_EVENT_SEC)
                is_overlap = False
                if last_end and target_start < last_end:
                    start_dt = last_end
                    is_overlap = True
                else:
                    start_dt = target_start

                end_dt = start_dt + timedelta(seconds=MOTION_CLIP_DURATION_SEC)
                cstate["last_recorded_end_dt"] = end_dt

            overlap_desc = f"⚡ Sambungan Klip Sebelumnya (Anti-Overlap Aktif, mulai {start_dt.strftime('%H:%M:%S')})" if is_overlap else f"Normal (Pre-event {MOTION_PRE_EVENT_SEC}s sebelum kejadian)"
            print(f"   📐 [MotionClip Boundary Ch {channel_num}] Rentang NVR: {start_dt.strftime('%H:%M:%S')} s/d {end_dt.strftime('%H:%M:%S')} ({MOTION_CLIP_DURATION_SEC}s) | Status: {overlap_desc}")

            # Konversi format waktu untuk NVR Playback Track:
            # Standar ISO UTC ("Z"): Konversi matematis jika ada timezone (misal +07:00 -> UTC 00:00)
            if dt_nvr.tzinfo is not None:
                start_dt_utc = start_dt.astimezone(timezone.utc)
                end_dt_utc = end_dt.astimezone(timezone.utc)
            else:
                start_dt_utc = start_dt
                end_dt_utc = end_dt

            s_utc_str = start_dt_utc.strftime("%Y%m%dT%H%M%SZ")
            e_utc_str = end_dt_utc.strftime("%Y%m%dT%H%M%SZ")

            # Waktu lokal NVR (tanpa huruf 'Z')
            s_loc_str = start_dt.strftime("%Y%m%dT%H%M%S")
            e_loc_str = end_dt.strftime("%Y%m%dT%H%M%S")

            # Prioritas kandidat pemotongan klip:
            # 1. Lapis 1A: Playback Track Main-Stream (01)
            # 2. Lapis 1B: Playback Track Sub-Stream (02)
            # 3. Lapis 2: Fallback RTSP Live Stream Main-Stream (01)
            cut_candidates = [
                ("Lapis 1A (Playback Main-Stream UTC)", f"rtsp://{NVR_USER}:{NVR_PASS}@{NVR_IP}:{NVR_PORT}/Streaming/tracks/{channel_num}01?starttime={s_utc_str}&endtime={e_utc_str}"),
                ("Lapis 1A (Playback Main-Stream Lokal)", f"rtsp://{NVR_USER}:{NVR_PASS}@{NVR_IP}:{NVR_PORT}/Streaming/tracks/{channel_num}01?starttime={s_loc_str}&endtime={e_loc_str}"),
                ("Lapis 1B (Playback Sub-Stream UTC)", f"rtsp://{NVR_USER}:{NVR_PASS}@{NVR_IP}:{NVR_PORT}/Streaming/tracks/{channel_num}02?starttime={s_utc_str}&endtime={e_utc_str}"),
                ("Lapis 2 (RTSP Live Stream 01)", f"rtsp://{NVR_USER}:{NVR_PASS}@{NVR_IP}:{NVR_PORT}/Streaming/Channels/{channel_num}01"),
            ]

            # Tunggu sejenak agar NVR selesai menulis detik kejadian ke disk
            time.sleep(MOTION_CLIP_DURATION_SEC + 1)

            # Ekstrak klip 10 detik
            unix_ms = str(int(time.time() * 1000))
            clip_filename = f"clip_{cam_info['token'][:6]}_ch{channel_num}_{unix_ms}.mp4"
            clip_path = os.path.join(TEMP_CLIPS_DIR, clip_filename)

            clip_success = False
            clip_tier_used = "Unknown"
            last_err_msg = "Unknown"

            if MOCK_MODE:
                # Simulasi klip dari file dummy cctv.kapal.mp4
                source_input = os.path.join(BASE_DIR, MOCK_VIDEO)
                mock_sec = int(time.time() % 15)
                cut_cmd = [
                    FFMPEG_CMD, "-y",
                    "-ss", f"00:00:{mock_sec:02d}",
                    "-i", source_input,
                    "-t", str(MOTION_CLIP_DURATION_SEC),
                    "-c:v", "copy",
                    "-an",
                    clip_path
                ]
                res = subprocess.run(cut_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                clip_success = res.returncode == 0 and os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000
                clip_tier_used = "Mock Mode"
            else:
                # Mode Asli: Playback Main -> Playback Sub -> Live Stream -> Jeda 5s -> Ulangi dari Awal
                max_attempts = 2
                for attempt in range(1, max_attempts + 1):
                    for tier_label, stream_url in cut_candidates:
                        cut_cmd = [
                            FFMPEG_CMD, "-y",
                            "-rtsp_transport", "tcp",
                            "-timeout", "5000000",
                            "-i", stream_url,
                            "-t", str(MOTION_CLIP_DURATION_SEC),
                            "-c:v", "copy",
                            "-an",
                            "-reset_timestamps", "1",
                            clip_path
                        ]
                        try:
                            res = subprocess.run(
                                cut_cmd,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE,
                                text=True,
                                timeout=MOTION_CLIP_DURATION_SEC + 8
                            )
                            if res.returncode == 0 and os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000:
                                clip_success = True
                                clip_tier_used = tier_label
                                break
                            else:
                                err_lines = [l.strip() for l in (res.stderr or "").splitlines() if l.strip()]
                                last_err_msg = err_lines[-1] if err_lines else f"Exit code {res.returncode}"
                        except subprocess.TimeoutExpired:
                            last_err_msg = "Timeout RTSP (NVR tidak merespons dalam 18s)"
                        except Exception as ex:
                            last_err_msg = str(ex)

                    if clip_success:
                        break

                    if attempt < max_attempts:
                        print(f"   ⏳ [MotionClip Ch {channel_num}] Belum berhasil potong klip (Status: {last_err_msg}). Jeda 5s lalu ulangi dari awal (Playback Main)...")
                        time.sleep(5)

            with self.lock:
                cstate = self._get_channel_state(channel_num)
                cstate["is_busy_recording"] = False
                if clip_success:
                    cstate["clips"].append(clip_path)
                    clip_size_kb = os.path.getsize(clip_path) / 1024.0
                    time_left = max(0, MOTION_WINDOW_SEC - (time.time() - cstate["window_start_time"]))
                    print(f"   🎬 [MotionClip Ch {channel_num}] Berhasil ({clip_tier_used}) potong klip ke-{len(cstate['clips'])}: {clip_filename} ({clip_size_kb:.1f} KB) | Sisa Window: {time_left:.0f}s")
                else:
                    print(f"   ⚠️ [MotionClip Ch {channel_num}] Gagal memotong klip rekaman NVR: {last_err_msg}")
        except Exception as e:
            with self.lock:
                cstate = self._get_channel_state(channel_num)
                cstate["is_busy_recording"] = False
            print(f"[MotionClip Error Ch {channel_num}] {e}")

    def check_and_finalize_windows(self):
        """Memeriksa apakah jendela 5 menit telah berakhir untuk diproses dan di-merge."""
        now_ts = time.time()
        for cam in CAMERAS:
            ch_num = cam["channel"]
            cam_token = cam["token"]
            cam_name = cam["name"]

            with self.lock:
                cstate = self._get_channel_state(ch_num)
                if cstate["state"] != "COLLECTING":
                    continue

                # Cek apakah sudah melewati batas window 5 menit (300 detik)
                if now_ts - cstate["window_start_time"] < MOTION_WINDOW_SEC:
                    continue

                # Window Selesai! Ambil daftar klip dan reset status
                clips_to_merge = list(cstate["clips"])
                thumb_to_use = cstate.get("thumbnail_path")
                cstate["state"] = "IDLE"
                cstate["clips"] = []
                cstate["thumbnail_path"] = None
                cstate["last_recorded_end_dt"] = None

            win_str = f"{MOTION_WINDOW_SEC // 60} Menit" if MOTION_WINDOW_SEC % 60 == 0 else f"{MOTION_WINDOW_SEC} Detik"
            if not clips_to_merge:
                print(f"[{datetime.now()}] [MotionWindow Ch {ch_num}] ⏰ Jendela {win_str} berakhir tanpa ada klip valid.")
                continue

            print(f"\n[{datetime.now()}] 🎬 [MotionWindow Ch {ch_num}] ⏰ JENDELA {win_str.upper()} BERAKHIR! Menggabungkan {len(clips_to_merge)} klip...")

            # Jalankan proses merge dan transcode di background thread
            threading.Thread(
                target=self._finalize_video_thread,
                args=(cam_token, cam_name, ch_num, clips_to_merge, thumb_to_use),
                daemon=True
            ).start()

    def _finalize_video_thread(self, cam_token: str, cam_name: str, ch_num: int, clips: list, thumb_to_use: str = None):
        """Menggabungkan potongan klip dan melakukan transcode ke 360p."""
        unix_ms = str(int(time.time() * 1000))
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        merged_raw_path = os.path.join(TEMP_CLIPS_DIR, f"merged_raw_{cam_token[:6]}_{unix_ms}.mp4")
        final_video_path = os.path.join(MOTION_VIDEOS_DIR, f"motion_{unix_ms}_{cam_token[:8]}.mp4")

        try:
            # 1. Gabungkan file MP4 dengan Stream Copy (-c copy) jika klip > 1
            if len(clips) == 1:
                merged_raw_path = clips[0]
                clip_sz = os.path.getsize(merged_raw_path) / 1024.0
                print(f"   🎬 [Video Batch Ch {ch_num}] Hanya 1 klip tunggal ({clip_sz:.1f} KB). Langsung menuju proses transcode 360p...")
            else:
                total_raw_size = sum(os.path.getsize(cp) for cp in clips if os.path.exists(cp)) / 1024.0
                print(f"   🔗 [Concat Engine Ch {ch_num}] Menggabungkan {len(clips)} klip akumulasi (Total mentah: {total_raw_size:.1f} KB) dengan FFMPEG Stream Copy (-c copy)...")
                list_txt_path = os.path.join(TEMP_CLIPS_DIR, f"list_{unix_ms}.txt")
                with open(list_txt_path, "w") as f:
                    for cp in clips:
                        f.write(f"file '{os.path.abspath(cp)}'\n")

                concat_cmd = [
                    FFMPEG_CMD, "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", list_txt_path,
                    "-c", "copy",
                    merged_raw_path
                ]
                res = subprocess.run(concat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    os.remove(list_txt_path)
                except Exception:
                    pass

                if res.returncode != 0 or not os.path.exists(merged_raw_path):
                    print(f"⚠️ [MotionWindow Ch {ch_num}] Gagal menggabungkan klip.")
                    return

            # 2. Transcode ke resolusi 360p (Hanya turunkan resolusi, visual tetap tajam)
            success = transcode_video_to_360p(merged_raw_path, final_video_path)

            # Bersihkan klip-klip sementara
            for cp in clips:
                try:
                    os.remove(cp)
                except Exception:
                    pass
            if len(clips) > 1 and os.path.exists(merged_raw_path):
                try:
                    os.remove(merged_raw_path)
                except Exception:
                    pass

            if not success or not os.path.exists(final_video_path):
                print(f"⚠️ [MotionWindow Ch {ch_num}] Gagal transcode ke 360p.")
                return

            final_size_kb = os.path.getsize(final_video_path) / 1024.0

            # 3. Gunakan Realtime Snapshot sebagai Thumbnail Poster WebP
            thumb_path = final_video_path.replace('.mp4', '_thumb.webp')
            t_size_kb = 0.0
            if thumb_to_use and os.path.exists(thumb_to_use):
                try:
                    shutil.copy2(thumb_to_use, thumb_path)
                    t_size_kb = os.path.getsize(thumb_path) / 1024.0
                    print(f"   🖼️ [Thumbnail Poster Ch {ch_num}] Snapshot kejadian dipasangkan sebagai cover video: {os.path.basename(thumb_path)} ({t_size_kb:.1f} KB)")
                except Exception:
                    pass

            # Fallback jika thumbnail snapshot belum ada: Ekstrak 1 frame dari video 360p
            if not os.path.exists(thumb_path):
                thumb_cmd = [
                    FFMPEG_CMD, "-y",
                    "-ss", "00:00:01",
                    "-i", final_video_path,
                    "-vframes", "1",
                    "-vf", "scale=360:270",
                    thumb_path
                ]
                try:
                    subprocess.run(thumb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(thumb_path):
                        t_size_kb = os.path.getsize(thumb_path) / 1024.0
                        print(f"   🖼️ [Thumbnail Poster Ch {ch_num}] Ekstraksi 1 frame WebP poster (360x270) siap: {os.path.basename(thumb_path)} ({t_size_kb:.1f} KB)")
                except Exception:
                    pass

            print(f"   🚀 [Queue Jalur 2] Video ({final_size_kb:.1f} KB) + Poster ({t_size_kb:.1f} KB) berhasil masuk antrean upload (/motions)!")

            # 4. Masukkan ke Antrean queue.db Jalur Video (event_type = 'motion_video')
            with db_lock:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute('''
                    INSERT INTO queue (camera_token, camera_name, file_path, captured_at, retry_count, is_uploading, event_type)
                    VALUES (?, ?, ?, ?, 0, 0, 'motion_video')
                ''', (cam_token, cam_name, final_video_path, now_str))
                conn.commit()
                conn.close()

        except Exception as e:
            print(f"[Finalize Video Error] {e}")

motion_window_mgr = MotionWindowManager()

# =============================================================================
# THREAD 1: ROUTINE SNAPSHOT WORKER (SETIAP 60 DETIK)
# =============================================================================
def routine_snapshot_worker():
    """Thread Pengambil Snapshot Rutin Berkala per Menit."""
    worker_name = "📸 SnapshotTimer"
    print(f"[{datetime.now()}] [{worker_name}] Memulai thread snapshot berkala ({SNAPSHOT_INTERVAL_SEC}s)...")

    cleanup_old_hd_snapshots()
    last_cleanup_date = datetime.now().date()

    while True:
        loop_start = time.time()
        current_date = datetime.now().date()
        if current_date != last_cleanup_date:
            cleanup_old_hd_snapshots()
            last_cleanup_date = current_date

        print(f"\n[{datetime.now()}] [{worker_name}] 🔄 Memulai siklus snapshot rutin...")
        for cam in CAMERAS:
            res = take_nvr_snapshot(cam, is_motion_event=False, event_type="snapshot")
            if res["success"]:
                print(f"   ✓ [Ch {cam['channel']}] {cam['name']}: {res['webp_size_kb']:.2f} KB (Antrean OK)")
            else:
                print(f"   ✗ [Ch {cam['channel']}] {cam['name']}: Gagal ({res.get('error')})")

        elapsed = time.time() - loop_start
        sleep_dur = max(0, SNAPSHOT_INTERVAL_SEC - elapsed)
        time.sleep(sleep_dur)

# =============================================================================
# THREAD 2: MULTI-BRAND MOTION EVENT LISTENER
# =============================================================================
def motion_event_listener_worker():
    """
    Thread Pemantau Event Motion dari NVR secara Realtime.
    Mendukung Hikvision AlertStream, Dahua CGI, dan ONVIF.
    Saat ada gerakan:
    1. Langsung jepret snapshot motion instan (<0.2s) & kirim ke Jalur 2.
    2. Masukkan ke MotionWindowManager untuk akumulasi video klip 5 menit.
    """
    worker_name = "🚨 MotionListener"
    last_motion_trigger = {}
    cam_by_channel = {cam["channel"]: cam for cam in CAMERAS}

    if NVR_BRAND == "hikvision":
        alert_stream_url = f"http://{NVR_IP}:{NVR_HTTP_PORT}/ISAPI/Event/notification/alertStream"
    elif NVR_BRAND == "dahua":
        alert_stream_url = f"http://{NVR_IP}:{NVR_HTTP_PORT}/cgi-bin/eventManager.cgi?action=attach&codes=[VideoMotion]"
    else:
        alert_stream_url = f"http://{NVR_IP}:{NVR_HTTP_PORT}/ISAPI/Event/notification/alertStream"

    while True:
        try:
            print(f"[{datetime.now()}] [{worker_name}] Menghubungkan ke NVR Event Stream ({NVR_BRAND.upper()} - {alert_stream_url})...")
            
            # Autentikasi Digest dengan Fallback Basic
            auth = HTTPDigestAuth(NVR_USER, NVR_PASS)
            res = requests.get(alert_stream_url, auth=auth, stream=True, timeout=(10, None))
            if res.status_code == 401:
                auth = HTTPBasicAuth(NVR_USER, NVR_PASS)
                res = requests.get(alert_stream_url, auth=auth, stream=True, timeout=(10, None))

            if res.status_code not in [200, 206]:
                print(f"[{datetime.now()}] [{worker_name}] ⚠️ Gagal buka stream NVR (HTTP {res.status_code}). Retry 10s...")
                time.sleep(10)
                continue

            print(f"[{datetime.now()}] [{worker_name}] ✅ Berhasil terhubung ke NVR Event Stream! Memantau pergerakan...")

            buffer = ""
            for chunk in res.iter_lines():
                if not chunk:
                    continue

                line = chunk.decode("utf-8", errors="ignore") if isinstance(chunk, bytes) else str(chunk)
                buffer += line + "\n"

                # Parse Event Hikvision
                if NVR_BRAND == "hikvision" and "</EventNotificationAlert>" in buffer:
                    xml_block = buffer
                    buffer = ""

                    is_motion = any(ev in xml_block for ev in [
                        "<eventType>VMD</eventType>",
                        "<eventType>linedetection</eventType>",
                        "<eventType>fielddetection</eventType>",
                        "<eventType>HumanDetection</eventType>",
                        "<eventType>intrusion</eventType>"
                    ])
                    is_active = "<eventState>active</eventState>" in xml_block or "<eventState>start</eventState>" in xml_block

                    if is_motion and is_active:
                        ch_match = re.search(r"<(?:dynChannelID|channelID)>(\d+)</(?:dynChannelID|channelID)>", xml_block)
                        channel_num = int(ch_match.group(1)) if ch_match else None

                        dt_match = re.search(r"<dateTime>([^<]+)</dateTime>", xml_block)
                        nvr_dt_str = dt_match.group(1) if dt_match else None

                        # Hanya proses jika nomor channel ini benar-benar terdaftar di config.json
                        if channel_num is not None and channel_num in cam_by_channel:
                            target_cam = cam_by_channel[channel_num]
                            now_ts = time.time()
                            last_ts = last_motion_trigger.get(channel_num, 0)

                            if now_ts - last_ts >= MOTION_COOLDOWN_SEC:
                                last_motion_trigger[channel_num] = now_ts
                                matched_event = next((ev for ev in ["VMD", "linedetection", "fielddetection", "HumanDetection", "intrusion"] if f"<eventType>{ev}</eventType>" in xml_block), "VMD")
                                print(f"\n⚡ [{worker_name}] 🚨 EVENT GERAKAN TERDETEKSI (Hikvision ISAPI)!")
                                print(f"   • Jenis Event : {matched_event} (State: active/start)")
                                print(f"   • Waktu NVR   : {nvr_dt_str if nvr_dt_str else 'N/A'}")
                                print(f"   • Kamera      : Ch {channel_num} ({target_cam['name']})")

                                # 1. SNAPSHOT THUMBNAIL: Hanya jepret 1x di awal kejadian per jendela 5 menit
                                thumb_path = None
                                if motion_window_mgr.should_capture_thumbnail(channel_num):
                                    if MOTION_CAPTURE_DELAY_SEC > 0:
                                        print(f"   • Delay Jepret: Menunggu {MOTION_CAPTURE_DELAY_SEC}s...")
                                        time.sleep(MOTION_CAPTURE_DELAY_SEC)
                                    m_res = take_nvr_snapshot(target_cam, is_motion_event=True, save_to_queue=False)
                                    thumb_path = m_res.get("webp_path") if m_res.get("success") else None

                                # 2. VIDEO 5 MENIT: Masukkan ke Window Manager
                                motion_window_mgr.on_motion_event(target_cam, nvr_dt_str, thumbnail_path=thumb_path)

                # Parse Event Dahua
                elif NVR_BRAND == "dahua" and "Code=VideoMotion" in buffer:
                    dahua_block = buffer
                    buffer = ""
                    if "action=Start" in dahua_block:
                        idx_match = re.search(r"index=(\d+)", dahua_block)
                        channel_num = int(idx_match.group(1)) + 1 if idx_match else None
                        if channel_num is not None and channel_num in cam_by_channel:
                            target_cam = cam_by_channel[channel_num]
                            print(f"\n⚡ [{worker_name}] 🚨 EVENT GERAKAN TERDETEKSI (Dahua CGI)!")
                            print(f"   • Jenis Event : VideoMotion (action: Start)")
                            print(f"   • Kamera      : Ch {channel_num} ({target_cam['name']})")
                            thumb_path = None
                            if motion_window_mgr.should_capture_thumbnail(channel_num):
                                m_res = take_nvr_snapshot(target_cam, is_motion_event=True, save_to_queue=False)
                                thumb_path = m_res.get("webp_path") if m_res.get("success") else None
                            motion_window_mgr.on_motion_event(target_cam, thumbnail_path=thumb_path)

        except requests.exceptions.RequestException as req_e:
            print(f"[{datetime.now()}] [{worker_name}] ⚠️ Koneksi Event Stream terputus ({req_e}). Reconnecting in 5s...")
            time.sleep(5)
        except Exception as e:
            print(f"[{worker_name}] [ERROR] {e}")
            time.sleep(5)

# =============================================================================
# THREAD 3A: ROUTINE SNAPSHOT UPLOADER WORKER (JALUR 1)
# =============================================================================
def upload_routine_worker(worker_id: int):
    """Thread Pengirim Snapshot Rutin WebP ke Server Darat (/snapshots)."""
    worker_name = f"⚡ Uploader-Routine-{worker_id}"
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=1)
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    while True:
        server_connected.wait()

        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''
                SELECT id, camera_token, camera_name, file_path, captured_at, retry_count 
                FROM queue 
                WHERE is_uploading = 0 AND event_type = 'snapshot'
                ORDER BY id ASC LIMIT 1
            ''')
            row = c.fetchone()
            if row:
                queue_id, camera_token, camera_name, file_path, captured_at, retry_count = row
                c.execute("UPDATE queue SET is_uploading = 1 WHERE id = ?", (queue_id,))
                conn.commit()
            conn.close()

        if not row:
            time.sleep(1.0)
            continue

        if not os.path.exists(file_path):
            with db_lock:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
                conn.commit()
                conn.close()
            continue

        url = f"{SERVER_BASE_URL.rstrip('/')}{SNAPSHOT_ENDPOINT_TMPL.format(cameraToken=camera_token)}"
        success = False
        status_code = None

        try:
            with open(file_path, 'rb') as img_f:
                files = {'file': (os.path.basename(file_path), img_f, 'image/webp')}
                data = {'captured_at': captured_at, 'camera_name': camera_name, 'event_type': 'snapshot'}
                res = session.post(url, files=files, data=data, timeout=(10, 25))
                status_code = res.status_code

            if status_code in [200, 201]:
                success = True
                print(f"  -> [{worker_name}] ✅ Upload Rutin Berhasil (HTTP {status_code}) | {os.path.basename(file_path)}")
            elif status_code in [400, 401, 403, 404, 422]:
                print(f"  -> [{worker_name}] ❌ HTTP {status_code} Client Rejection! Auto-Purge Queue ID {queue_id}")
                with db_lock:
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
                    conn.commit()
                    conn.close()
                try:
                    os.remove(file_path)
                except Exception:
                    pass
                continue

        except Exception as e:
            pass

        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            if success:
                conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
                conn.commit()
                conn.close()
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            else:
                # Gagal jaringan / 502 / timeout -> Simpan dan jangan dihapus!
                server_connected.clear()
                conn.execute("UPDATE queue SET is_uploading = 0, retry_count = retry_count + 1 WHERE id = ?", (queue_id,))
                conn.commit()
                conn.close()
                time.sleep(2)

# =============================================================================
# THREAD 3B: MOTION VIDEO UPLOADER WORKER (JALUR 2 - VIDEO 5 MENIT + THUMBNAIL)
# =============================================================================
def upload_motion_video_worker(worker_id: int):
    """Thread Pengirim Video Gabungan 5 Menit ke Server Darat (/motions)."""
    worker_name = f"🎬 Uploader-MotionVid-{worker_id}"
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=1)
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    while True:
        server_connected.wait()

        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''
                SELECT id, camera_token, camera_name, file_path, captured_at, retry_count 
                FROM queue 
                WHERE is_uploading = 0 AND event_type = 'motion_video'
                ORDER BY id ASC LIMIT 1
            ''')
            row = c.fetchone()
            if row:
                queue_id, camera_token, camera_name, file_path, captured_at, retry_count = row
                c.execute("UPDATE queue SET is_uploading = 1 WHERE id = ?", (queue_id,))
                conn.commit()
            conn.close()

        if not row:
            time.sleep(2.0)
            continue

        if not os.path.exists(file_path):
            with db_lock:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
                conn.commit()
                conn.close()
            continue

        url = f"{SERVER_BASE_URL.rstrip('/')}{MOTION_VIDEO_ENDPOINT_TMPL.format(cameraToken=camera_token)}"
        success = False
        status_code = None

        thumb_path = file_path.replace('.mp4', '_thumb.webp')

        try:
            v_size_kb = os.path.getsize(file_path) / 1024.0
            print(f"[{datetime.now()}] [{worker_name}] Mengunggah video motion 360p ({v_size_kb:.1f} KB) ke {url}...")
            with open(file_path, 'rb') as vid_f:
                files = {'file': (os.path.basename(file_path), vid_f, 'video/mp4')}
                thumb_f = None
                if os.path.exists(thumb_path):
                    thumb_f = open(thumb_path, 'rb')
                    files['thumbnail'] = (os.path.basename(thumb_path), thumb_f, 'image/webp')

                data = {
                    'captured_at': captured_at,
                    'recorded_at': captured_at,
                    'cameraToken': camera_token,
                    'camera_token': camera_token,
                    'camera_name': camera_name,
                    'event_type': 'motion_video',
                    'is_motion': '1'
                }
                res = session.post(url, files=files, data=data, timeout=(15, 60))
                status_code = res.status_code

                if thumb_f:
                    try:
                        thumb_f.close()
                    except Exception:
                        pass

            if status_code in [200, 201]:
                success = True
                print(f"  -> [{worker_name}] 🎬 ✅ Upload Video Motion + Thumbnail Berhasil (HTTP {status_code}) | {os.path.basename(file_path)}")
            elif status_code in [400, 401, 403, 404, 422]:
                print(f"  -> [{worker_name}] ❌ HTTP {status_code} Client Rejection! Auto-Purge Queue ID {queue_id}")
                with db_lock:
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
                    conn.commit()
                    conn.close()
                try:
                    os.remove(file_path)
                except Exception:
                    pass
                if os.path.exists(thumb_path):
                    try:
                        os.remove(thumb_path)
                    except Exception:
                        pass
                continue

        except Exception as e:
            pass

        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            if success:
                conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
                conn.commit()
                conn.close()
                try:
                    os.remove(file_path)
                except Exception:
                    pass
                if os.path.exists(thumb_path):
                    try:
                        os.remove(thumb_path)
                    except Exception:
                        pass
            else:
                server_connected.clear()
                conn.execute("UPDATE queue SET is_uploading = 0, retry_count = retry_count + 1 WHERE id = ?", (queue_id,))
                conn.commit()
                conn.close()
                time.sleep(3)

# =============================================================================
# THREAD 4: VIDEO WINDOW WATCHER DAEMON (TIMER 5 MENIT)
# =============================================================================
def video_window_watcher_worker():
    """Thread Pemantau Siklus Jendela 5 Menit."""
    while True:
        try:
            motion_window_mgr.check_and_finalize_windows()
        except Exception as e:
            print(f"[Window Watcher Error] {e}")
        time.sleep(1.0)

# =============================================================================
# THREAD 5: CONNECTION HEALTH CHECK WORKER
# =============================================================================
def connection_check_worker():
    """Thread Pengecek Status Koneksi ke Server Darat."""
    worker_name = "🌐 HealthCheck"
    session = requests.Session()
    health_url = SERVER_BASE_URL.rstrip('/')

    while True:
        try:
            res = session.get(health_url, timeout=5)
            if res.status_code in [200, 404, 405, 401]:
                if not server_connected.is_set():
                    print(f"[{datetime.now()}] [{worker_name}] 🟢 Terhubung ke Server Darat! Mengaktifkan semua Uploader...")
                    server_connected.set()
            else:
                if server_connected.is_set():
                    print(f"[{datetime.now()}] [{worker_name}] 🔴 Server Darat tidak sehat (HTTP {res.status_code}). Uploader dijeda...")
                    server_connected.clear()
        except Exception:
            if server_connected.is_set():
                print(f"[{datetime.now()}] [{worker_name}] 🔴 Koneksi ke Server Darat terputus. Uploader dijeda...")
                server_connected.clear()

        time.sleep(CONNECTION_CHECK_INTERVAL)

# =============================================================================
# MAIN ORCHESTRATOR
# =============================================================================
def main():
    print("=" * 75)
    print(" 🚢 MARITIME CCTV AGENT - DUAL-LANE (ROUTINE SNAPSHOT & MOTION VIDEO ALERT)")
    print(f" NVR IP      : {NVR_IP}:{NVR_PORT} (HTTP: {NVR_HTTP_PORT}) | Brand: {NVR_BRAND.upper()}")
    print(f" Server URL  : {SERVER_BASE_URL}")
    print(f" Kamera      : {len(CAMERAS)} Unit CCTV Aktif")
    win_str = f"{MOTION_WINDOW_SEC // 60} Menit" if MOTION_WINDOW_SEC % 60 == 0 else f"{MOTION_WINDOW_SEC}s"
    print(f" Windowing   : {MOTION_WINDOW_SEC}s ({win_str}) | Klip: {MOTION_CLIP_DURATION_SEC}s | Pre-event: {MOTION_PRE_EVENT_SEC}s")
    print(f" VA-API Dev  : {VAAPI_DEVICE} (Ada: {os.path.exists(VAAPI_DEVICE)})")
    print(f" Mock Mode   : {'AKTIF (' + MOCK_VIDEO + ')' if MOCK_MODE else 'NON-AKTIF (Live NVR)'}")
    print("=" * 75)

    init_db()

    # Jalankan Pengecek Koneksi Awal
    print("[Main] Memeriksa koneksi awal ke server darat...")
    try:
        r = requests.get(SERVER_BASE_URL.rstrip('/'), timeout=5)
        if r.status_code in [200, 404, 405, 401]:
            server_connected.set()
            print("[Main] 🟢 Koneksi awal ke Server Darat TERHUBUNG!")
        else:
            print(f"[Main] ⚠️ Server merespons HTTP {r.status_code}. Menunggu healthcheck...")
    except Exception as e:
        print(f"[Main] ⚠️ Belum terhubung ke server ({e}). Uploader menunggu sinyal...")

    threads = []

    # 1. Thread Snapshot Timer Rutin
    t_routine = threading.Thread(target=routine_snapshot_worker, daemon=True, name="Thread-RoutineSnapshot")
    threads.append(t_routine)

    # 2. Thread Motion Event Listener
    t_motion = threading.Thread(target=motion_event_listener_worker, daemon=True, name="Thread-MotionListener")
    threads.append(t_motion)

    # 3. Thread Video Window Watcher (Timer 5 Menit)
    t_watcher = threading.Thread(target=video_window_watcher_worker, daemon=True, name="Thread-WindowWatcher")
    threads.append(t_watcher)

    # 4. Thread Connection Health Check
    t_health = threading.Thread(target=connection_check_worker, daemon=True, name="Thread-HealthCheck")
    threads.append(t_health)

    # 5. Worker Pool Jalur 1: Routine Snapshot Uploaders (1 worker per kamera)
    for i in range(len(CAMERAS)):
        t_up_routine = threading.Thread(target=upload_routine_worker, args=(i+1,), daemon=True, name=f"Thread-UploaderRoutine-{i+1}")
        threads.append(t_up_routine)

    # 6. Worker Pool Jalur 2: Dedicated Motion Video Uploader (1 worker)
    t_up_mvid = threading.Thread(target=upload_motion_video_worker, args=(1,), daemon=True, name="Thread-UploaderMotionVid-1")
    threads.append(t_up_mvid)

    for t in threads:
        t.start()

    print(f"\n[Main] 🚀 Semua {len(threads)} thread operasional telah aktif!")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Main] 🛑 Menerima sinyal stop. Menghentikan Maritime CCTV Agent...")
        sys.exit(0)

if __name__ == "__main__":
    main()
