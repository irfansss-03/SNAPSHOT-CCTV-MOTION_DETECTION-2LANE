# 🚢 Maritime NVR CCTV Snapshot, Realtime Motion & Video Alert Agent (Tri-Lane Edition)

Dokumentasi teknis dan panduan operasional resmi untuk **Maritime CCTV Snapshot, Realtime Motion & Video Alert Agent (Tri-Lane Architecture)**. Sistem ini dirancang khusus untuk lingkungan maritim (kapal laut) dengan koneksi internet terbatas (Satelit VSAT / 4G Pesisir), menggabungkan tiga jalur pemantauan visual terpadu: **Snapshot Rutin Berkala**, **Snapshot Gerakan Realtime Instan**, dan **Klip Video Rangkuman Kejadian (360p) + Thumbnail Poster**.

---

## 📌 Daftar Isi
1. [Arsitektur Sistem Tri-Lane (3 Jalur API)](#1-arsitektur-sistem-tri-lane-3-jalur-api)
2. [Fitur Utama & Keunggulan Maritim](#2-fitur-utama--keunggulan-maritim)
3. [Multi-Brand Native Driver Engine (Tier 1 s/d Tier 4)](#3-multi-brand-native-driver-engine)
4. [Mekanisme Video Windowing & Anti-Overlapping Formula](#4-mekanisme-video-windowing--anti-overlapping-formula)
5. [Akselerasi Grafis Hardware Intel VA-API & Transcode 360p](#5-akselerasi-grafis-hardware-intel-va-api--transcode-360p)
6. [Kontrak API Server Darat (REST API)](#6-kontrak-api-server-darat-rest-api)
7. [Struktur Konfigurasi Dinamis (config.json)](#7-struktur-konfigurasi-dinamis-configjson)
8. [Panduan Instalasi & Eksekusi Otomatis (setup.sh)](#8-panduan-instalasi--eksekusi-otomatis-setupsh)
9. [Troubleshooting & Solusi Kendala Lapangan](#9-troubleshooting--solusi-kendala-lapangan)

---

## 1. Arsitektur Sistem Tri-Lane (3 Jalur API)

```text
                       ┌──────────────────────────────────────────────────────────┐
                       │            HIKVISION / DAHUA / ONVIF NVR (KAPAL)         │
                       └─────────────┬──────────────────────────────┬─────────────┘
                                     │                              │
                     Port 80 (ISAPI Native HTTP API)       Port 554 (RTSP Playback Track)
                                     │                              │
                     ┌───────────────┴───────────────┐              │
                     │                               │              │
                     ▼                               ▼              ▼
           ┌───────────────────┐           ┌───────────────────┐ ┌───────────────────┐
           │ JALUR 1: RUTIN    │           │ JALUR 2: INSTAN   │ │ JALUR 3: BUFFER   │
           │ Snapshot 60s      │           │ Motion Snapshot   │ │ Windowing Video   │
           │ (ISAPI /picture)  │           │ (ISAPI /picture)  │ │ (10s Cut & Merge) │
           └─────────┬─────────┘           └─────────┬─────────┘ └─────────┬─────────┘
                     │                               │                     │
                     ▼                               ▼                     ▼
           ┌───────────────────┐           ┌───────────────────┐ ┌───────────────────┐
           │  WebP Kompresi    │           │  WebP Kompresi    │ │ 1. Transcode 360p │
           │  (< 10 KB, 360x270│           │  (< 10 KB, 360x270│ │ 2. WebP Thumbnail │
           └─────────┬─────────┘           └─────────┬─────────┘ └─────────┬─────────┘
                     │                               │                     │
                     └───────────────────────┬───────┴─────────────────────┘
                                             │
                                             ▼
                           ┌───────────────────────────────────┐
                           │   SQLite WAL Queue (queue.db)     │
                           └─────────────────┬─────────────────┘
                                             │
                 ┌───────────────────────────┼───────────────────────────┐
                 │                           │                           │
                 ▼                           ▼                           ▼
     ┌───────────────────────┐   ┌───────────────────────┐   ┌───────────────────────┐
     │   UPLOADER JALUR 1    │   │   UPLOADER JALUR 2    │   │   UPLOADER JALUR 3    │
     │    Snapshot Rutin     │   │    Motion Snapshot    │   │     Motion Video      │
     └───────────┬───────────┘   └───────────┬───────────┘   └───────────┬───────────┘
                 │                           │                           │
                 ▼                           ▼                           ▼
     POST .../snapshots          POST .../motion-snapshots   POST .../motions
     (File + captured_at)        (File + captured_at)        (File + Thumbnail + Token)
```

---

## 2. Fitur Utama & Keunggulan Maritim

1. **Tri-Lane Zero-Blocking Architecture**:
   - **Jalur 1 (Rutin)**: Mengunggah snapshot periodik (60s) ke `/snapshots`.
   - **Jalur 2 (Motion Snapshot)**: Mengunggah foto bukti gerakan seketika ($\le 0.3$ detik) ke `/motion-snapshots`.
   - **Jalur 3 (Motion Video)**: Mengakumulasikan klip kejadian selama rentang waktu window (misal 5 menit), di-transcode ke 360p, lalu diunggah ke `/motions` lengkap dengan poster thumbnail.
2. **Koneksi Satelit Super Hemat**:
   - Foto WebP selalu berada di bawah **`< 10 KB`** (rata-rata 9.0 – 9.2 KB).
   - Video kejadian 30 detik beresolusi 360p hanya memakan bandwidth **`< 1 MB`** (960 KB).
3. **Ketahanan Jaringan (*Store & Forward Offline-First*)**:
   - Jika kapal berada di area *blank spot* tanpa sinyal satelit/4G, semua snapshot dan video ditampung aman di SQLite WAL lokal (`queue.db`).
   - Begitu sinyal tersambung kembali, seluruh antrean otomatis terkirim berurutan tanpa ada data yang hilang.
4. **Auto-Purge Error 400/401/403/404/422**:
   - Token kamera kadaluarsa atau URL server tidak valid otomatis dibersihkan dari antrean agar tidak menimbulkan kebuntuan (*deadlock*).
   - Gangguan jaringan (*Connection Timeout / Network Unreachable*) **tidak akan pernah dihapus** dan akan terus dicoba ulang sampai berhasil.

---

## 3. Multi-Brand Native Driver Engine

Sistem dilengkapi mekanisme 4-tier fallback cerdas yang kompatibel dengan berbagai merk NVR / IP Camera:

* **Tier 1 (Hikvision ISAPI Native)**: `GET /ISAPI/Streaming/channels/{ch}01/picture` (Response instan **0.13s – 0.20s**).
* **Tier 2 (Dahua CGI Native)**: `GET /cgi-bin/snapshot.cgi?channel={ch}`.
* **Tier 3 (ONVIF PullPoint Native)**: Standard Snapshot URI ONVIF XML.
* **Tier 4 (Universal RTSP Fallback)**: Mengambil I-Frame tunggal via koneksi FFmpeg TCP RTSP.

---

## 4. Mekanisme Video Windowing & Anti-Overlapping Formula

Untuk mencegah ledakan file video di server, kejadian gerakan di-buffer dalam jendela waktu (default 5 menit / 300 detik):

1. **Pemotongan Klip 10 Detik dari NVR**:
   - Menggunakan URL Playback Track NVR:
     `rtsp://user:pass@ip:port/Streaming/tracks/{ch}01?starttime=...&endtime=...`
2. **Rumus Anti-Tumpang Tindih (*Anti-Overlapping Boundary*)**:
   $$\text{Start Time} = \max(\text{Alert} - 3\text{s},\ \text{Last Recorded End})$$
   Jika gerakan terjadi terus-menerus tanpa jeda, detik rekaman klip berikutnya **langsung menyambung tepat di akhir klip sebelumnya**. Tidak ada video dobel, dan tidak ada detik kejadian yang terpotong.
3. **Stream Copy Concat**:
   Semua klip 10 detik di dalam jendela waktu digabungkan menggunakan FFmpeg `-c copy` (tanpa re-encode, super cepat).

---

## 5. Akselerasi Grafis Hardware Intel VA-API & Transcode 360p

Sistem secara otomatis mendeteksi ketersediaan GPU Intel (misal Intel Atom CherryView, Celeron Braswell, Core i-series pada Axiomtek / Asus Mini PC):

* **Deteksi Otomatis Node**: Menguji keberadaan `/dev/dri/renderD128`.
* **Hardware Encoder**: Menggunakan `h264_vaapi` dengan filter `scale_vaapi=w=640:h=-2` (Beban CPU nyaris 0%).
* **Auto-Fallback Cerdas**: Jika driver VA-API belum aktif atau device tidak tersedia, sistem otomatis beralih ke software CPU FFMPEG (`libx264 -preset veryfast -crf 23`) dengan standar kualitas visual jernih.
* **Thumbnail Poster Extraction**: Otomatis mengekstrak frame pertama menjadi gambar poster WebP 360x270 berukuran $\approx 18\text{ KB}$.

---

## 6. Kontrak API Server Darat (REST API)

### 📸 1. Endpoint Snapshot Rutin (Per-Menit)
* **Method**: `POST`
* **URL**: `{base_url}/cctv/worker/cameras/{cameraToken}/snapshots`
* **Header**: `Content-Type: multipart/form-data`
* **Form-Data**:
  * `captured_at`: `2026-09-08T08:44:28.463Z` (ISO-8601 UTC)
  * `file`: `[binary image/webp]` (< 10 KB, Resolusi 360x270)

### 🚨 2. Endpoint Motion Snapshot Instan (Realtime Alert)
* **Method**: `POST`
* **URL**: `{base_url}/cctv/worker/cameras/{cameraToken}/motion-snapshots`
* **Header**: `Content-Type: multipart/form-data`
* **Form-Data** *(Identik dengan Snapshot Rutin)*:
  * `captured_at`: `2026-09-08T08:32:29.000Z` (ISO-8601 UTC)
  * `file`: `[binary image/webp]` (< 10 KB, Resolusi 360x270)

### 🎬 3. Endpoint Motion Video (Batch Windowing)
* **Method**: `POST`
* **URL**: `{base_url}/cctv/worker/cameras/{cameraToken}/motions`
* **Header**: `Content-Type: multipart/form-data`
* **Form-Data**:
  * `file`: `[binary video/mp4]` (360p H.264 Faststart)
  * `thumbnail`: `[binary image/webp]` (Poster 360x270 px)
  * `captured_at`: `2026-09-08T08:32:26.000Z` (ISO-8601 UTC)
  * `cameraToken`: `{cameraToken}`

---

## 7. Struktur Konfigurasi Dinamis (config.json)

```json
{
    "nvr": {
        "ip": "192.168.1.100",
        "port": 554,
        "http_port": 80,
        "user": "admin",
        "pass": "password_nvr",
        "brand": "hikvision"
    },
    "server": {
        "base_url": "https://api.vsemar.com/api/v1",
        "snapshot_endpoint": "/cctv/worker/cameras/{cameraToken}/snapshots",
        "motion_snapshot_endpoint": "/cctv/worker/cameras/{cameraToken}/motion-snapshots",
        "motion_video_endpoint": "/cctv/worker/cameras/{cameraToken}/motions"
    },
    "agent": {
        "snapshot_interval_sec": 60,
        "connection_check_interval_sec": 60,
        "hd_retention_days": 30,
        "motion_window_sec": 300,
        "motion_clip_duration_sec": 10,
        "motion_pre_event_sec": 3,
        "motion_cooldown_sec": 3,
        "motion_capture_delay_sec": 0.0,
        "vaapi_device": "/dev/dri/renderD128",
        "video_scale_w": 640,
        "video_fps": 15,
        "mock_mode": false,
        "mock_video": "cctv.kapal.mp4"
    },
    "cameras": [
        {
            "channel": 1,
            "name": "Kamera Haluan",
            "token": "5dL4C7t44nfC5IqkKJkQBh4kUBQqEg3h"
        }
    ]
}
```

---

## 8. Panduan Instalasi & Eksekusi Otomatis (setup.sh)

### 🚀 Instalasi 1 Perintah:
```bash
# Masuk sebagai root sekali saja untuk auto-setup permission & driver
su -
cd /path/to/SNAPSHOT-CCTV-MOTION_DETECTION-3LANE
bash setup.sh
```

Skrip `setup.sh` secara otomatis akan:
1. Mendaftarkan user host ke file `/etc/sudoers.d/` (`NOPASSWD`).
2. Memasukkan user ke grup **`render`** dan **`video`** untuk akses GPU Intel VA-API.
3. Menginstall dependensi sistem (`ffmpeg`, `webp`, `sqlite3`, `btop`, `python3`, `va-driver-all`, `vainfo`).
4. Menginstall **NetBird VPN** untuk remote access maritim.
5. Memasang library Python (`requests`, `opencv-python-headless`, `numpy`, `pillow`).
6. Membuat service background `cctv-snapshot.service` (Systemd).

### ⚙️ Menjalankan Layanan:
```bash
# 1. Hubungkan NetBird Remote VPN
netbird up --setup-key [SETUP_KEY]

# 2. Uji Coba Manual di Terminal
python3 snapshotcompress.py

# 3. Aktifkan & Jalankan Sebagai Background Service
sudo systemctl enable cctv-snapshot
sudo systemctl start cctv-snapshot

# 4. Monitoring Realtime
sudo systemctl status cctv-snapshot
tail -f logs/agent_$(date +%Y-%m-%d).log
btop
```

---

## 9. Troubleshooting & Solusi Kendala Lapangan

1. **Uji Coba Driver Intel VA-API di Mini PC**:
   ```bash
   vainfo
   ```
   Pastikan baris `VAProfileH264Main : VAEntrypointEncSlice` muncul.
2. **Kamera Tumbang/Reboot Saat Potong Klip**:
   * Periksa apakah perangkat adalah IP Camera standalone (tanpa hard disk). Fitur playback tracks `/Streaming/tracks/` membutuhkan NVR dengan media penyimpanan (HDD/SD Card).
3. **Pembersihan Database & Antrean Bersih Total**:
   ```bash
   rm -rf queue.db* logs/* temp_clips/* motion_videos/* snapshots_nvr_4cctv/*
   ```

---
*Developed for Maritime Fleet Intelligence & Surveillance Operations — PT Semar Nusantara.*
