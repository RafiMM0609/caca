# Audit: kenapa build Toko Roti makan ~56 menit

Job: `df2d015acd184d01ab58354afc19d5e0` (build, **succeeded**)
Project: `08e9b8657d2e48cdb2afc64ed048bad3` — Toko Roti Sederhana (`nuxt-studio`, revision 6)
Wall clock: **22:39:41 → 23:35:36 = 3.355,3 s = 55,9 menit**

Sumber: tabel `jobs`/`workers`/`events` di `data/factory.sqlite3`,
`data/jobs/df2d015a…/workers.json` + `verification.txt`, dan artefak `/tmp` yang
ditinggalkan worker (log build, screenshot, log chrome).

Catatan penting: perbaikan dari commit `2b2b037` ("Size the worker clock to the work")
sudah aktif di job ini (commit 22:35, job mulai 22:39). Efeknya terlihat: **semua task
attempt 1, tidak ada timeout, tidak ada retry, review approve di percobaan pertama.**

---

## Waterfall waktu

| Tahap | Detik | Menit | % job |
| --- | ---: | ---: | ---: |
| Wave 1 (`scaffold-nuxt-app`) | 859,3 | 14,3 | 25,6% |
| Wave 2 (`home-page-ui` + `health-api`) | 2.358,4 | 39,3 | 70,3% |
| Verification (build + 2 acceptance case) | 136,6 | 2,3 | 4,1% |

Per worker:

| Task | Skills | Detik | Menit | % job |
| --- | --- | ---: | ---: | ---: |
| `scaffold-nuxt-app` | — | 677,2 | 11,3 | 20,2% |
| **`home-page-ui`** | `design-taste-frontend` | **2.170,4** | **36,2** | **64,7%** |
| `health-api` | — | 101,7 | 1,7 | 3,0% |
| **Total kerja worker** | | 2.949,3 | 49,2 | 87,9% |
| Sisa (review + merge + verify) | | 406,0 | 6,8 | 12,1% |

Jadi ~88% waktu job adalah satu worker menulis satu halaman. Sisanya kecil.

---

## Top 3 penyebab

### 1. Loop iterasi desain di `home-page-ui` — 36,2 menit (64,7% dari job)

Bukan model yang lambat, bukan `npm`. Diukur di host ini: `npm ci` ~29 s, `npm run build`
(Nuxt) ~19 s. Bukti jejak di `/tmp` selama 22:54–23:31:

- **2 build produksi penuh** yang tercatat: `build2.log` (23:24) dan
  `build-final.log` (23:28), masing-masing satu siklus `Building Nuxt for production`
  (~19 s client + ~2 s server). Ditambah beberapa kali restart dev server, jadi
  siklus build-jalankan-lihat terulang sepanjang 36 menit, bukan sekali di akhir.
- **16 pass screenshot** — `taste-shoot-1024/768/390`, `taste-shoot2-{1024,1440,390}`,
  `taste-shoot3-{1024,1440,390}`, `taste-shoot-final`, `taste-shoot-ui/2/3`,
  `taste-consistency-ui`, `taste-consistency-ui2`, `shots` → **41 file PNG**.
- Server dev dinyalakan berulang di dalam window-nya: `bakery-headers.txt` +
  `bakery-home.html` (23:04), `home2/3/4/final.html` (23:19–23:29), dan
  `bakery-server.log`/`.pid` (23:29) mendengarkan di port **3311**.

Catatan atribusi: `server.log`/`dev.log`/`dev2.log` (22:47–22:50) **bukan** milik worker
ini — itu jatuh di window `scaffold-nuxt-app` (22:39:42–22:53:58). Di sana worker scaffold
bahkan menabrak `ERROR Pre-transform error: Failed to resolve import "#app-manifest"`
di port 4318, lalu pindah ke 4319 (`dev2.log`) sampai jalan. Port 4318/4319 milik
scaffold, bukan homepage.

Angka yang bisa saya buktikan dari artefak adalah 2 build produksi + 16 pass visual.
Siklus dev-server di antaranya tidak tercatat durasinya, jadi 36 menit itu tidak bisa
saya pecah menjadi "X menit build" dan "Y menit melihat" — yang bisa dikatakan adalah
36 menit itu habis di dalam loop tersebut, dan produk akhirnya satu file `pages/index.vue`
plus 6 gambar.

Transcript worker (4000 char terakhir) memperlihatkan waktunya habis bukan di coding,
tapi di **musyawarah pemilihan gambar**: membandingkan kandidat foto
`m33603672` vs `m33603558` vs `c166` vs `m37164269`, menilai mana yang "round loaves"
vs "bread in oven", lalu memutuskan tetap pakai set sekarang. Kalimat penutupnya:
*"Keep current. The photos are a process/detail set."*

Penilaian: loop ini **memang yang membuat output bagus** — reviewer memberi 13 temuan
positif (palette, type scale, reduced-motion, focus-visible, tidak ada
`transition: all`, responsive 360 px aman). Tapi tidak ada satu pun dari 36 menit itu
yang di-budget secara eksplisit, dan tidak ada checkpoint yang memberi tahu worker
"cukup, kunci desainnya".

**Bisa dipotong:** beri batas jumlah pass visual (mis. 3 pass screenshot), dan
pindahkan pemilihan aset gambar ke **planner** (aset sudah tersedia di
`skills/design-taste-frontend/assets`; planner yang memilih, worker tinggal memakai)
sehingga worker tidak mengulang deliberasi yang sama.

### 2. `FACTORY_WORKER_CONCURRENCY=1` mematikan paralelisme DAG — wave 2 jalan seri

`data/secrets.env:6` berisi `FACTORY_WORKER_CONCURRENCY=1`. Default di
`factory/config.py` adalah 3.

Buktinya di tabel `workers`:

| Task | created | started |
| --- | --- | --- |
| `home-page-ui` | 22:54:01 | 22:54:01 |
| `health-api` | **23:31:22** | 23:31:22 |

`health-api` bukan "menunggu lalu mulai" — row-nya **baru dibuat 37 menit kemudian**,
setelah `home-page-ui` selesai. Padahal keduanya satu wave (`wave.start` 22:54:01
mengumumkan `["home-page-ui", "health-api"]`, keduanya hanya bergantung pada
`scaffold-nuxt-app`). Jadi DAG paralel yang dirancang di `factory/models.py::ready_set`
tidak pernah terjadi di runtime.

Konsekuensi jujur: ini hanya membuang **101,7 s (1,7 menit)** kerja langsung.
Wave 2 = 39,3 menit untuk pekerjaan seri 38,0 menit. Dengan concurrency 3, wave 2 akan
≈ 37,6 menit (yang terlama + overhead), jadi hemat ≈ 1,7 menit. Jadi ini **bukan**
penyebab utama 54 menit — tapi ini yang membuat setiap penambahan task di wave yang
sama menambah wall clock penuh, bukan sebagian. Dan konsisten dengan catatan di
`worker-wall-clock.md` bahwa 2 build Nuxt konkuren OOM di host 2 GB; jadi ini batasan
memori, bukan nilai config. Kalau host tetap 2 GB, angka ini tidak akan berubah.

### 3. Friksi lingkungan browser di dalam loop desain — sandbox Chrome gagal, bukan biner hilang

Fix ke-3 (`PLAYWRIGHT_BROWSERS_PATH`) bekerja: worker menemukan Chromium di
`~/.cache/ms-playwright/chromium-1248`. Tapi **binernya tidak bisa jalan**:

`/tmp/chrome-manual.log`:
```
FATAL:zygote_host_impl_linux.cc:130] No usable sandbox! ... disabled unprivileged
user namespaces with AppArmor ... you can try using --no-sandbox
Received signal 6
```

Worker lalu harus menemukan sendiri workaround-nya dan menulis
`/tmp/chrome-nosandbox.sh` berisi `exec .../chrome --no-sandbox "$@"` (23:07), untuk
mengatasi crash `Received signal 6` di atas.

Sebelum itu, pada 23:05 worker mencoba 5 viewport lewat `/usr/bin/chromium-browser`
(`chrome-desktop|phone|tablet|phone-small|tablet-portrait.log`). Jujur soal statusnya:
log itu **penuh noise** `dbus/bus.cc:405 Failed to connect to the bus` dan
`xdg-settings: not found`, tapi screenshot-nya tetap jadi (`taste-shoot-ui*` 23:06–23:08).
Jadi dbus bukan penyebab kegagalan — itu hanya membuat output worker sulit dibaca.
Yang benar-benar memblokir adalah `No usable sandbox` di `chrome-manual.log` (23:07),
dan itu yang memaksa worker menulis wrapper `--no-sandbox` sendiri.

Jadi fix ke-3 memindahkan masalah: dari "biner tidak terlihat" menjadi "biner terlihat
tapi tidak bisa launch". Bottleneck-nya berpindah ke AppArmor/no-sandbox, yang harus
ditemukan ulang oleh setiap worker di setiap attempt.

**Bisa dipotong:** set flag no-sandbox sekali di `worker_env`
(`factory/agents.py::worker_env`) alih-alih membiarkan setiap worker menemukannya ulang,
dan redam chatter dbus. Catatan: ini hanya menghapus beberapa menit eksplorasi
lingkungan — bukan 36 menitnya. Penyebab #1 tetap loop desainnya.

---

## Yang sudah benar dan jangan diubah

- Tidak ada timeout, tidak ada retry, tidak ada repair di job ini — perbaikan
  worker clock `2b2b037` berhasil (bandingkan build #1 `f43d70d0`: 65,9 menit, attempt
  1 menghabiskan 1802 s lalu di-cut).
- `health-api` hanya 101,7 s untuk satu file — biaya model bukan masalah.
- Verification 136,6 s (build + 2 case lulus) — murah, pertahankan.
- Menurunkan `npm ci`/build tidak relevan: itu < 5% dari 56 menit.

## Kalau hanya boleh satu perbaikan

Batasi loop desain. 36 dari 56 menit ada di satu worker yang memverifikasi ulang
keputusan visual yang sudah ia ambil sendiri. Menambah clock dari 1800 s ke 3600 s
(yang sudah dilakukan) menghentikan pemotongan, tapi tidak menghentikan pengulangan.
