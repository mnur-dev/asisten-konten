# Asisten Konten — catatan proyek

Aplikasi lokal untuk membuat video papan catur yang **timing-nya mengikuti video
siaran**, bukan jam di PGN. Tujuan akhirnya menggantikan pekerjaan manual:
menonton pertandingan penuh (10–30 menit) sambil menekan tombol "next" di papan
analisis lalu merekam layar.

Semua berjalan lokal di Windows. Tidak ada VPS.

```bash
python -m app          # buka http://127.0.0.1:8420
```

**Server tidak punya auto-reload.** Setelah mengubah kode di `core/` atau `app/`,
hentikan prosesnya (`Stop-Process` pada PID yang listen di port 8420) lalu jalankan
ulang. Perubahan `app/ui/index.html` cukup hard refresh browser (Ctrl+Shift+R).

## Alur kerja

1. **Buat proyek** — link video (diunduh otomatis lewat yt-dlp) + teks PGN
2. **Deteksi** — baca papan overlay digital di video, cocokkan tiap frame ke posisi PGN
3. **Pilih papan fisik** — klik 4 sudut papan kayu (searah jarum jam) lalu tarik tiap
   titik untuk mengikuti sudut kamera; bukan kotak tegak lurus. Manual, sekali per
   video (lihat catatan di bawah). Disimpan sebagai `board_quad`, direktifikasi lewat
   filter `perspective` ffmpeg sebelum dianalisis di `core/physical.py`.
4. **Ikuti papan fisik** — geser waktu tiap ply dari overlay ke papan kayu
5. **Review** — putar rentang 10 detik, papan kanan melangkah ikut waktu video, koreksi manual
6. **Tata letak** (opsional) — semua penempatan di `full-video.mp4` ditandai manual
   dengan menarik kotak di atas satu frame contoh. Panel "Tata letak" di UI punya
   beberapa layer, semuanya memakai picker yang sama:
   - `paste_rect` — posisi papan hasil render. Kosong = pakai posisi papan overlay
     hasil deteksi. Papan menjaga rasio aslinya dan diletakkan di tengah kotak;
     jangan diregangkan, sebab dengan eval bar aktif `board.mp4` lebih lebar
     daripada tinggi.
   - `logo_rects` — dihapus dengan `delogo` (interpolasi piksel sekitar, bukan AI)
   - `blur_rects` — disamarkan dengan `boxblur`
   - `brand_file` + `brand_rect` — logo milik pengguna yang diunggah, ditempel
   - `name_rects` — papan nama pemain putih/hitam, teks dari PGN
7. **Render** — `board.mp4` + `full-video.mp4` (video asli, papan overlay-nya ditimpa
   video board kita; audio asli dibisukan, yang terdengar cuma klik langkah kalau
   suara langkah aktif).

**delogo vs blur — jangan tertukar.** `delogo` menebak isi kotak dari piksel di
sekelilingnya, jadi hanya meyakinkan untuk tanda **diam** di latar relatif polos.
Elemen yang berubah tiap frame (eval bar bawaan siaran, jam, ticker) akan jadi noda
bergerak kalau di-`delogo`; itu yang dipakai `blur_rects`. Sudah diverifikasi di frame
nyata: delogo memang bekerja dengan benar untuk logo statis.

**Logo dan nama pemain digambar di PIL, bukan `drawtext`.** `core/render.furniture_layer()`
membuat satu PNG RGBA seukuran frame berisi logo + papan nama, lalu ditimpa sekali
sebagai input ffmpeg terakhir. Alasannya: `drawtext` butuh path font di dalam string
filter, dan di Windows path itu mengandung titik dua drive plus backslash yang harus
lolos dua lapis escaping — rapuh dan sulit dilacak kalau salah. PIL juga menyamakan
kendali tipografinya dengan papan yang sudah digambar PIL.

## Struktur

```
app/main.py        FastAPI localhost, tanpa auth. Deteksi/render jalan di thread.
app/ui/index.html  satu halaman, vanilla JS, tanpa build step
core/pgn.py        parse PGN + signature okupansi 8x8 per ply
core/video.py      helper ffmpeg (unduh via yt-dlp, probe, sampling gray)
core/overlay.py    lokalisasi papan overlay + baca isi kotak
core/align.py      DP monoton frame -> ply
core/detect.py     orkestrasi deteksi overlay
core/physical.py   re-timing dari papan kayu (changepoint)
core/render.py     tema, eval bar, render concat-demuxer, overlay ke video asli,
                   furniture_layer (logo + nama pemain sebagai PNG RGBA)
core/pieces.py     rasterisasi SVG bidak lewat pycairo
core/audio.py      suara langkah dari klip di assets/sounds/ + mux
core/evaluation.py sumber evaluasi (PGN [%eval] atau engine UCI)
projects/<id>/     meta.json, input.pgn, timestamps.json, evals.json, *.mp4, log.txt
```

`controller/`, `worker/`, `renderer/`, `shared/`, `tests/` adalah **sistem VPS lama
yang sudah digantikan** dan belum dihapus. Menunggu konfirmasi pengguna sebelum
dihapus lewat commit.

## Temuan terukur (jangan diturunkan ulang)

**Deteksi overlay bekerja sangat baik.** Papan overlay dicocokkan ke posisi PGN
lewat okupansi 64 kotak. Di video Carlsen–Gao, 32/32 ply dengan cost 0 (semua
kotak cocok persis). Di Murzin, 68/69 ply.

- Okupansi kotak = `std piksel > 20` — terbukti 64/64 sempurna
- Warna bidak = `fraksi piksel < 70` dengan ambang `0.269` — error 0,14% dari 5550 sampel
- Rata-rata piksel **tidak bisa** dipakai untuk warna: bidak putih di kotak terang
  punya mean hampir sama dengan kotak kosong

**Metrik diff global tidak akan pernah bekerja.** Satu bidak = 2 dari 64 kotak.
Dirata-rata ke seluruh papan, perubahannya larut. Diukur di video nyata: langkah
asli menghasilkan diff 2–7, sedangkan zoom kamera 55 dan potongan 129. Sinyal
8–25× lebih kecil dari noise. Ini sebab sistem lama gagal.

**Overlay tertinggal dari papan fisik.** Median 0,6 detik, tapi di time scramble
bisa 4–5 detik (contoh terukur: 29.Kd2 overlay 452,00 vs papan fisik 447,40).
Karena itu re-timing papan fisik ada.

**Lokalisasi papan fisik otomatis GAGAL.** Tiga pendekatan dicoba dan ketiganya
menemukan pemain, bukan papan: peta perubahan persisten, rasio waktu-langkah vs
waktu-acak, dan kriteria sparsity. Penyebabnya pemain berganti postur secara
permanen sementara bidak kayu kontrasnya rendah. **Area papan ditandai manual di
UI** (4 sudut, bukan kotak tegak lurus — lihat alur kerja di atas). Jangan ulangi
eksperimen ini tanpa ide yang benar-benar baru.

**Overlay kadang menampilkan papan lain.** Di turnamen beregu grafisnya bergiliran
antar papan. Sekitar 4 dari 18 frame kalibrasi tidak cocok. Karena itu ada verify
gate: sumber apa pun harus membuktikan diri terhadap PGN sebelum dipercaya.

## Engine

Stockfish 18 (build `bmi2`, cocok untuk Intel Kaby Lake) terpasang di
`engines/stockfish/stockfish-windows-x86-64-bmi2.exe` dan terdeteksi otomatis
oleh `evaluation.find_engine()`. Folder `engines/` di-gitignore — kalau repo
di-clone ulang, unduh lagi dari rilis resmi `official-stockfish/Stockfish`.

Evaluasi 69 ply memakan ~20 detik pada `movetime=0.25` dengan 3 thread.

## Jebakan yang sudah pernah menggigit

- **`re-time` harus idempoten.** Selalu acu ke `overlay_timestamp` yang tersimpan,
  jangan ke `timestamp` saat ini. Pernah bug: run kedua memakai hasil run pertama
  sebagai acuan dan merusak hasil diam-diam.
- **Ply yang sudah diedit manual tidak boleh ditimpa** oleh re-time.
- **pycairo di Windows me-link cairo secara statis** ke dalam `.pyd`. `cairosvg`,
  `cairocffi`, dan `rlPyCairo` semuanya gagal mencari DLL. Karena itu `core/pieces.py`
  menggambar SVG langsung ke pycairo.
- **Polling status UI harus berhenti** saat pekerjaan selesai, kalau tidak redraw
  tiap 1,5 detik memutus pemutar video di panel review.
- **`-vsync` tidak dikenal** oleh ffmpeg versi ini; pakai `-fps_mode`.
- Render lama memompa frame mentah ke pipe (336 GB untuk video 30 menit 1080p).
  Sekarang concat demuxer: satu PNG per ply, selesai dalam hitungan detik.
- **`align.solve()` bisa "mulai" path-nya di ply mana pun pada frame 0, gratis.**
  Selama papan overlay belum tampil di layar, cost ke SEMUA ply sama-sama di-cap
  (`CAP=6`, tidak bawa informasi). Karena diam di satu ply tidak lebih murah dari
  langsung mulai di ply berikutnya, DP-nya sering memilih ply 1 sejak frame 0 —
  padahal belum ada bukti apa pun di sana — dan waktu langkah pertama jadi 0 detik
  meski papan overlay-nya sendiri baru muncul jauh belakangan. `align.waypoints()`
  sekarang cuma memakai frame pertama yang cost-nya di bawah `CAP` (benar-benar
  informatif) untuk `timestamp`, bukan sekadar frame pertama di sepanjang segmen
  yang ditempati path. Cuma ply pertama yang biasanya kena — ply lain selalu
  "dijaga" oleh bukti kuat ply sebelumnya.

## Yang belum selesai

- **Klasifikasi langkah** ala chess.com (Brilliant/Great/Blunder): butuh MultiPV
  2–3 plus cek pengorbanan materi. Ambang chess.com tidak dipublikasikan; rencana
  memakai rumus win% Lichess sebagai pendekatan. Ini pekerjaan berikutnya.
- **Hapus sistem VPS lama** setelah pengguna puas dengan yang baru.
- **Test otomatis untuk `core/`** belum ada. `tests/` yang ada menguji sistem lama.
- Pemutaran video di panel review belum pernah diverifikasi di browser sungguhan
  (panel headless tidak meng-compose frame sehingga video ter-suspend).

## Bahasa

Pengguna berkomunikasi dalam bahasa Indonesia. Balas dalam bahasa Indonesia.
Komentar dan nama di dalam kode tetap bahasa Inggris.
