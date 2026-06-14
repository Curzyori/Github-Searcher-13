# Github Searcher (13)

**Github Searcher (13)** adalah alat baris perintah (CLI) berbasis Python yang dirancang untuk mencari repositori dan memindai kode di GitHub secara asinkron. Alat ini dibangun sebagai proyek kolaborasi antara **@Curzyori** dan **@Seeyaa77**.

## Problem
Mencari kode atau repositori dalam skala besar di GitHub sering kali terhambat oleh batasan rate limit API resmi (HTTP 403) serta keharusan autentikasi yang rumit saat melakukan pencarian mendalam terhadap ribuan file secara cepat.

## Solution
Aplikasi ini menawarkan arsitektur ganda (**Dual-Engine**):
- **Auto Pilot**: Menyimulasikan sesi browser menggunakan cookie session (`GITHUB_SESSION`) untuk merayap (scrape) antarmuka web pencarian GitHub, sehingga mampu meminimalkan batasan API konvensional.
- **Fast Skip**: Memanfaatkan token akses personal (`GITHUB_TOKEN`) untuk melakukan pencarian langsung melalui API resmi GitHub dengan proses yang cepat dan ringkas.

## Features
- 🚀 **Dual Engine Mode**: Mendukung mode *Auto Pilot* (Sesi Browser) & *Fast Skip* (Token API).
- 🔍 **Search Repositories**: Mencari dan mengekstrak daftar repositori GitHub secara massal dengan output yang terstruktur.
- 📝 **Search Code**: Memindai kode sumber pada repositori terpilih untuk mencari string atau token spesifik.
- ⚡ **Asynchronous Batch Scanning**: Proses pemindaian berjalan secara asinkron dalam kelompok (batch) berisi 5 repositori sekaligus demi kecepatan maksimal.
- 🔑 **Interactive 2FA/OTP**: Mendukung verifikasi login dua langkah (Two-Factor Authentication) langsung dari terminal.
- 💾 **Auto Session Saving**: Menyimpan session cookie secara otomatis ke berkas `.env` setelah berhasil login.

## Demo
```text
╔══════════════════════════════════════════════════════════╗
║                ⚙️  [GITHUB SEARCHER (13)]                 ║
║         Collab Project: @Curzyori x @Seeyaa77            ║
╚══════════════════════════════════════════════════════════╝

  1. Auto Pilot  -> Sesi Browser (.env Session)
  2. Fast Skip   -> Direct Token (GITHUB_TOKEN)

  [?] Pilih Mode Eksekusi [1/2]: 
```

## Tech Stack
- **Python (Asyncio)** — Menjalankan tugas I/O secara non-blocking untuk performa pencarian asinkron yang superior.
- **HTTPX** — Klien HTTP modern untuk mengirimkan permintaan asinkron dan mengelola cookie sesi dengan mudah.
- **BeautifulSoup4** — Melakukan parsing dokumen HTML dari hasil pencarian web GitHub pada mode Auto Pilot.
- **Aiofiles** — Menulis hasil pemindaian dan token yang ditemukan ke filesystem secara asinkron tanpa menghambat jalannya program.
- **Python-dotenv** — Membaca dan memperbarui berkas konfigurasi `.env` secara dinamis.

## Getting Started

### 1. Clone Repository
```bash
git clone https://github.com/Curzyori/Github-Searcher-13.git
cd Github-Searcher-13
```

### 2. Install Dependencies

**Linux & macOS:**
```bash
pip3 install -r requirements.txt
```

**Windows:**
```cmd
pip install -r requirements.txt
```

### 3. Setup Environment Variables

**Linux & macOS:**
```bash
cp .env.example .env
```

**Windows:**
```cmd
copy .env.example .env
```

### 4. Run Application

**Linux & macOS:**
```bash
python3 main.py
```

**Windows:**
```cmd
python main.py
```

## Usage
1. Buka aplikasi dengan menjalankan perintah `python3 main.py` (Linux/macOS) atau `python main.py` (Windows).
2. Pilih Mode Eksekusi:
   - Pilih `1` (Auto Pilot) jika ingin masuk menggunakan akun GitHub (akan ditanya email & password, serta kode 2FA jika aktif). Sesi akan disimpan otomatis.
   - Pilih `2` (Fast Skip) untuk menggunakan token GitHub yang sudah didefinisikan di berkas `.env`.
3. Gunakan menu CLI:
   - Menu `1`: Masukkan kata kunci pencarian repositori dan jumlah halaman hasil pencarian yang ingin diekstrak.
   - Menu `2`: Masukkan kata kunci pencarian kode (seperti nama fungsi, API, atau secret token) serta filter ekstensi berkas.

## Configuration
Isi konfigurasi pada berkas `.env`:
```env
GITHUB_TOKEN=ghp_yourtokendisini
GITHUB_SESSION=cookie_user_session_otomatis_terisi
```

## Roadmap
- [ ] Perbaikan fitur **CARI KODE** pada **mode 1 (Auto Pilot)**.
- [ ] Dukungan rotasi proxy untuk menghindari rate-limit IP pada mode Auto Pilot.
- [ ] Antarmuka pemilihan pola regex pencarian kode secara kustom dari menu CLI.
- [ ] Fitur ekspor hasil pencarian ke format JSON dan CSV.

## License
Di bawah lisensi [MIT License](https://github.com/Curzyori/Github-Searcher-13/blob/main/LICENSE). Hak Cipta (c) 2026 @Curzyori & @Seeyaa77.
