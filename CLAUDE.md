# Asisten Konten — catatan proyek

Aplikasi lokal untuk membuat video papan catur yang **timing-nya mengikuti video
siaran**, bukan jam di PGN. Tujuan akhirnya menggantikan pekerjaan manual:
menonton pertandingan penuh (10–30 menit) sambil menekan tombol "next" di papan
analisis lalu merekam layar.

Dikembangkan dan dipakai lokal di Windows; salinannya juga di-deploy ke VPS
(`https://yt.sukaweb.my.id/chess-vids/`) — lihat **Deploy** di bawah.

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
**Satu preview untuk semua pengaturan.** Tombol "Preview & atur" membuka satu kartu
(`previewCard()`) dengan tab: Zoom · Papan render · Hapus logo · Blur · Logo saya · Nama
putih · Nama hitam · Papan fisik · Thumbnail · Short. Tab video panjang, papan fisik, dan
frame thumbnail memakai **satu** gambar + scrubber (`pickerBody()`, id `pick*`) dengan
waktu bersama (`pickAt`), jadi pindah tab tetap di detik yang sama. Kotak layer lain
digambar putus-putus (`drawGhosts()`, dipetakan lewat crop zoom kalau frame yang tampil
tidak di-zoom). Pengecualian: editor judul thumbnail menggambar di atas hasil AI (id
`thumb*`), dan tab Short menukar isinya dengan editor 9:16 miliknya sendiri, karena
keduanya memang gambar yang berbeda. `setPreviewTab()` yang mengatur `picking`/`layer`;
`toggleShort()`/`toggleThumb()`/`setLayer()` tinggal pembungkusnya.

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
   Frame contohnya dipilih lewat scrubber yang sama dengan picker frame thumbnail
   (`wireScrubber()`): slider sepanjang video + tombol putar. Selama diputar/digeser
   yang tampil `<video>` sumber (di-crop CSS mengikuti `source_zoom`), begitu berhenti
   diganti JPEG `/frame` detik itu — kotak selalu digambar di atas still dari backend.
   Di HP semua picker (`#pickwrap`) jalan lewat jembatan sentuh→mouse di akhir script:
   sentuh-tahan 280 ms lalu geser = klik-kiri-tahan; usapan langsung tetap scroll
   halaman; ketuk = klik. Picker baru di dalam `#pickwrap` otomatis ikut.
   Kotak yang sudah ada (tersimpan maupun baru digambar) bisa digeser lewat
   `wireBoxDrag()` — tata letak, kotak judul thumbnail; short punya
   `wireShortBoxDrag()` sendiri. Kotak tersimpan langsung disimpan saat dilepas.
   Akibatnya kotak baru harus mulai digambar di luar kotak yang sudah ada.
   Rentang slider preview tata letak = jendela video jadi (`outputWindow()`: langkah 1
   − mulai → langkah terakhir + selesai, dari nilai kotak yang sedang terisi, belum
   perlu disimpan). Di bawahnya `renderPickInfo()` menampilkan posisi di video jadi,
   sisa durasi, dan angka "selesai +" / "mulai −" supaya video berhenti/mulai di detik
   yang sedang tampil; tombol "Pakai" langsung menyimpannya. Sebelum deteksi (belum ada
   timestamp) slider tetap sepanjang siaran.
7. **Render video panjang** — `board.mp4` + `full-video.mp4` (video asli, papan
   overlay-nya ditimpa video board kita; audio asli dibisukan, yang terdengar cuma
   klik langkah kalau suara langkah aktif). Dimulai di langkah pertama, bukan di
   detik 0 — lihat `lead_in` di bawah.
8. **Render short** — `short.mp4` (9:16, ply-ply terakhir saja) lewat tombolnya
   sendiri; video panjang tidak ikut dibuat.
9. **Thumbnail** (opsional) — satu frame video dipilih manual, dibersihkan lewat
   model gambar (logo & papan digital hilang, pemain dimajukan), judulnya digambar
   PIL di atasnya. Satu-satunya bagian berbayar di aplikasi ini — lihat di bawah.

**Balik papan (`flip_board`).** Tombol "⇅ balik papan" di panel Tampilan menaruh
hitam di bawah. Yang ikut terbalik bukan cuma petaknya: koordinat, papan jam (sisi
dekat selalu plat kiri), dan eval bar. Eval bar butuh **dua** hal dibalik bersamaan —
proporsinya (`1 - share`) *dan* kedua warnanya (`near`/`far` di `draw_eval_bar()`).
Membalik proporsi saja membuat ujung bawah tetap dicat putih padahal yang diukur di
situ hitam; ini sudah pernah salah sekali. Karena mengubah piksel `board.mp4`,
`flip_board` masuk `board_look_of()`.

**Video panjang mulai di langkah pertama (`lead_in`).** Siaran punya menit-menit
pembuka yang tidak ada isinya — pemain duduk, wasit bicara, papan belum jalan; di 13
proyek yang ada jarak langkah pertamanya 3–182 detik. `full-video.mp4` sekarang mulai
di langkah 1, dan kotak "mulai −" di sebelah tombolnya menyisakan sekian detik siaran
sebelum itu (0 = langsung di langkah 1, 60 = satu menit sebelumnya). Nilainya disimpan
di `meta.json` sebagai `lead_in` dan di-post ulang tepat sebelum render, seperti
short-cut, supaya angka yang dirender selalu angka yang terlihat di kotaknya.

Titik mulainya `max(0, waktu langkah 1 − lead_in)`, jadi minta lead-in lebih panjang
daripada yang dipunya siaran cuma berarti mulai dari 0. Pemotongan dilakukan di
`overlay_composite()` lewat `-ss` yang **sama** untuk kedua input — siaran dan
`board.mp4` sama-sama berjalan di jam siaran (`plan[0]` menahan posisi awal sampai
langkah pertama mendarat), jadi satu seek menggeser keduanya berbarengan. Sengaja
tidak dipotong di `plan`: `board.mp4` harus tetap utuh dari detik 0 karena short
memakainya ulang dan menghitung dari awal file. Karena itu juga `lead_in` **tidak**
masuk `board_look_of()` — ia tidak mengubah satu piksel pun di `board.mp4`.

**Video panjang berhenti di `outro` detik setelah langkah terakhir.** Kotak "selesai +"
di sebelah "mulai −" (default 60, 0 = berhenti tepat di langkah terakhir), disimpan di
`meta.json` sebagai `outro` dan di-post ulang sebelum render seperti `lead_in`. Ini
**menggantikan** aturan lama `HOLD_AFTER_BOARD` (board.mp4 habis + 60 dtk tetap, ≈ langkah
terakhir + 63 dtk). Waktu langkah terakhir = jumlah semua durasi `plan` kecuali entri
terakhir (hold 3 dtk penutup board.mp4). `overlay_composite(end=...)` membekukan frame
terakhir board.mp4 (`tpad` clone) selama perlu lalu memotong dengan `-t end − start`;
tetap dibatasi panjang siaran. Terukur di video sintetis: mulai 2, selesai 15 → 13,03 dtk
dengan papan terakhir masih tampil; selesai melebihi siaran 30 dtk → 30,0 dtk.

**`-ss` yang jatuh sedikit di atas batas frame menggeser video satu frame, audio
tidak.** Hasilnya klik langkah berjalan satu frame mendahului gambar. Terukur di
proyek nyata: `-ss 5.233333` memberi `start_time` video 0,033 s sementara audio 0,000;
`-ss 5.233000` memberi 0,000 di keduanya. Karena itu `overlay_composite()` membulatkan
titik mulai **ke bawah** ke grid `BOARD_FPS` lalu menggesernya 1 ms lagi ke bawah.
Membulatkan ke bawah juga yang membuatnya tidak pernah memotong langkah pertama.

Sinkronnya sudah diverifikasi, bukan dikira-kira: render yang sama dengan `start=0`
dan `start=5,25` dibandingkan frame-per-frame, dan wilayah papan kami maupun wilayah
siaran sama-sama paling cocok di offset yang **sama** — keduanya tetap sejalan.

**Dua tombol render terpisah.** Video panjang dan short punya tombol dan endpoint
masing-masing (`/render` dan `/short`), sebab keduanya diulang karena alasan berbeda:
panjang karena layout/logo berubah, short karena potongan ply/jeda/ekor atau caption
diubah. Keduanya butuh `board.mp4`; `/short` memakai ulang board dari render
sebelumnya — itu yang membuat coba-coba potongan memakan hitungan detik, bukan menit.
Board dibuat ulang oleh `/short` hanya kalau filenya belum ada atau tampilannya sudah
basi. "Basi" diputuskan `board_look_of()`: tema, set bidak, eval bar, jam, suara, plus
rencana durasi per ply, disimpan di `meta.json` sebagai `board_look` setiap kali board
dirender. Tanpa itu, ganti tema lalu render short saja akan diam-diam menumpuk caption
baru di atas papan lama. Proyek lama yang board-nya dibuat sebelum `board_look` ada
tetap dipakai ulang (asal-usulnya tidak diketahui, jangan paksa rebuild).

**Thumbnail (`core/thumbnail.py`).** Satu frame siaran yang dipilih pengguna, dibersihkan
oleh model gambar, lalu judulnya digambar PIL di atasnya. Keluarannya `thumbnail.png`
tepat 1280x720 — ukuran thumbnail YouTube, dan kebetulan juga ukuran sah untuk API-nya
(kedua sisi kelipatan 16, 921.600 piksel ada di dalam jendela 655.360–8.294.400), jadi
tidak ada crop susulan.

**Gambar ke AI, teks ke PIL — pembagian ini yang membuat fiturnya murah dipakai.**
Menghapus logo dan papan digital siaran bukan pekerjaan filter: `delogo` menebak dari
piksel sekeliling, sedangkan yang dibutuhkan di sini adalah mengarang ulang latar di
belakang benda yang dihapus, plus memperbesar dan memajukan kedua pemain. Itu memang
hanya bisa model gambar. Tapi teksnya tidak: kalau judul ikut di-generate, ganti satu
kata berarti panggilan berbayar baru **dan** wajah yang tergambar ulang sedikit berbeda,
jadi judul tidak akan pernah bisa dicoba-coba tanpa gambarnya ikut melayang. Digambar
lokal, ganti judul/ukuran/posisi itu gratis, seketika, identik piksel antar percobaan,
dan tidak pernah salah eja — ejaan adalah satu-satunya hal yang masih sering meleset di
model gambar. Terukur: `/thumb-texts` + `/thumb-compose` selesai dalam 62 ms.

Alur di panel "Thumbnail": geser ke detik yang ekspresinya paling kuat → **Pakai frame
ini** (membekukan `thumb-source.png` resolusi penuh) → prompt (nama pemain diisi dari
PGN) → **Buat dengan AI** → tarik kotak judul di hasilnya. Frame dibekukan ke disk
lebih dulu, bukan diambil ulang saat generate, supaya percobaan kedua mengirim frame
yang sama persis dengan yang sudah disetujui.

**Tiap percobaan disimpan, tidak ditimpa** (`thumb-ai-1.png`, `-2.png`, …). Percobaan
yang lebih baru sering lebih jelek daripada yang lama, dan tiap gambar itu sudah dibayar
— kembali ke yang lama tidak boleh berarti membayar lagi. Menghapusnya selalu lewat
konfirmasi, tidak pernah otomatis.

**Prompt hanya disimpan kalau pengguna mengeditnya.** `thumb_prompt_of()` mengembalikan
`DEFAULT_PROMPT` selama `meta["thumb_prompt"]` kosong, jadi memperbaiki prompt bawaan
ikut sampai ke semua proyek yang belum pernah menyentuh miliknya, sementara proyek yang
prompt-nya sudah disetel tangan tetap persis seperti yang disetel.

**Lewat OpenRouter, bukan langsung ke OpenAI.** Satu key, satu bentuk request, 52
model gambar di belakangnya. Alasannya bukan harga — tarifnya diteruskan apa adanya
(sudah dicocokkan ke halaman harga resmi kedua penyedia: GPT Image 2.5 $30/1M token
output, Gemini 3 Pro Image $120/1M, sama persis) — melainkan karena pertanyaan
sebenarnya di sini adalah "model termurah mana yang sanggup mengerjakan edit ini",
dan itu dijawab dengan mencoba enam model, bukan memilih satu di depan. Lewat
OpenRouter, ganti model itu satu dropdown; kalau langsung ke penyedia, tiap penyedia
berarti endpoint dan bentuk request sendiri.

Endpoint-nya `POST /api/v1/images`: JSON biasa, frame masuk sebagai data URL di
`input_references`. Yang diminta **aspect ratio 16:9, bukan ukuran piksel** — semua
model paham 16:9, tidak semua menerima `1280x720` — lalu `compose()` menormalkan
hasilnya ke `THUMB_SIZE` apa pun yang dikembalikan model.

**Biaya nyata datang dari balasannya sendiri.** `usage.cost` memuat dolar yang
benar-benar ditagih untuk panggilan itu, jadi `meta["thumb_usage"]` mengakumulasi
angka sungguhan dan panel tidak perlu menebak. Harga di dropdown tetap perkiraan dan
ditandai begitu: OpenRouter menyebut satuan tiap tagihan (`image`, `megapixel`,
`token`), dan yang bersatuan **token tidak bisa dipastikan di muka** — pada model
OpenAI jumlah tokennya berayun ~11x antara `quality` low dan max. Yang bersatuan
`image` atau `megapixel` pasti.

**Parameter yang diterima berbeda tiap model, jadi catatan modelnya yang memutuskan.**
Sebagian model tidak punya `quality` sama sekali (Gemini, FLUX), sebagian punya
himpunan sendiri (`gpt-image-1-mini` berhenti di `high`, `gpt-image-2.5-*` sampai
`max`). Mengirim nilai yang tidak dikenal adalah request yang **ditolak**, bukan
field yang diabaikan — karena itu `thumb_model_record()` membaca `supported_parameters`
dari daftar model, dan `quality` dikirim hanya kalau model itu memilikinya.

Daftar model di-cache ke `openrouter-models.json` (gitignored) karena harganya ada di
catatan per-model: menyusun daftar berarti satu request per model. Di-refetch kalau
cache hilang, lebih tua dari `CACHE_DAYS`, atau tombol "Segarkan" ditekan — model baru
muncul sendiri tanpa menyentuh kode, dan itu justru inti memakai OpenRouter.

**Key-nya di `.env`, dibaca ulang tiap panggilan.** `.env` sudah di-gitignore. Dibaca
tiap panggilan, bukan di-cache saat import, sebab server ini tidak punya auto-reload —
menyuruh pengguna me-restart server untuk memakai key yang baru saja mereka tempel
adalah kesan pertama yang buruk.

Pesan error diteruskan apa adanya, sebab parameter yang tidak didukung, nama model
yang salah, atau saldo habis hanya bisa dikenali dari kalimatnya sendiri.

**Klasifikasi langkah (`core/classify.py`).** Nilai tiap langkah ala chess.com:
`brilliant`, `great`, `best`, `excellent`, `good`, `inaccuracy`, `mistake`, `blunder`.
Diukur di **kurva win% Lichess**, bukan centipawn — selisih 100 cp tidak berarti apa-apa
di +9 tapi menentukan di 0.00. Tiga ambang Lichess yang dipublikasikan (10 / 20 / 30 poin
win% yang dilepas) menandai inaccuracy/mistake/blunder; di atasnya `excellent` (<2) dan
`good` (<10) adalah pilihan kita sendiri. Ambang chess.com tidak dipublikasikan, jadi
namanya saja yang dipinjam.

`best` dan `great` butuh langkah terbaik versi engine, jadi hanya muncul lewat jalur
`analyse_game()`. PGN yang cuma punya `[%eval]` tetap dapat semua nilai lain — posisi
sebelum langkah 1 diisi `OPENING_SCORE` karena tidak ada PGN yang menuliskannya.

`brilliant` satu-satunya yang melihat papan, bukan skor: pengorbanan materi yang tetap
lolos pemeriksaan engine. Materinya dihitung lewat **static exchange evaluation** sendiri
(`see()`, swap-off dijalankan di salinan papan sungguhan supaya baterai di belakang
penyerang pertama ikut terbuka dan bidak yang ter-pin tidak dihitung sebagai pembela).
Syaratnya: ≥1,5 bidak dikorbankan, rugi ≤4 poin win%, setelahnya tidak kalah (≥40%),
sebelumnya belum menang telak (≤90%, sekitar +6,0), dan ada lebih dari satu langkah legal.

Hasil disimpan di `moves.json`, ditulis **bersamaan** dengan `evals.json` oleh
`store_analysis()` — keduanya lahir dari angka yang sama dan tidak boleh berbeda umur.
Badge digambar di petak tujuan langkah (`render.BADGES`); `excellent` dan `good` sengaja
tidak punya badge, sebab berdua mereka mengisi hampir seluruh partai kuat dan menandai
semuanya justru menenggelamkan dua yang penting.

**Badge menyala secara default** (`main.badges_on()`, `meta.get("move_badge", True)`).
Kunci yang hilang berarti default, jadi proyek lama ikut kebagian; tombol "!! badge" di
panel Tampilan menulis `False` secara eksplisit dan **itu selalu menang**, jadi yang
sengaja dimatikan tetap mati. Semua jalur membaca lewat `badges_on()` — jangan baca
`meta["move_badge"]` langsung, sebab kunci yang absen akan terbaca mati.

Kunci `move_badge`/`badges` masuk `board_look_of()` hanya kalau badge menyala **dan** ada
grade untuk digambar. Dua-duanya perlu: tanpa grade, board keluar sama persis dengan board
sebelum fitur ini ada, dan menambahkan kuncinya cuma akan menyuruh tombol short membangun
ulang board demi perubahan yang tidak mengecat apa pun. Waktu defaultnya dibalik ke menyala,
9 dari 11 proyek jadi basi — itu memang maunya (board mereka belum ada badge-nya), dan dua
sisanya tanpa `board_look` tetap dipakai ulang apa adanya.

**delogo vs blur — jangan tertukar.** `delogo` menebak isi kotak dari piksel di
sekelilingnya, jadi hanya meyakinkan untuk tanda **diam** di latar relatif polos.
Elemen yang berubah tiap frame (eval bar bawaan siaran, jam, ticker) akan jadi noda
bergerak kalau di-`delogo`; itu yang dipakai `blur_rects`. Sudah diverifikasi di frame
nyata: delogo memang bekerja dengan benar untuk logo statis.

**Jam diambil dari `[%clk]` di PGN, bukan dibaca dari layar.** `pgn.clock_series()`
memberi pasangan (putih, hitam) untuk tiap indeks ply; `[%clk]` menyatakan sisa waktu
pihak yang **baru saja** melangkah, jadi pihak lain tetap menampilkan bacaan terakhirnya
sendiri. Sebelum ada langkah, nilai awal diambil dari header `TimeControl`. Jam hanya
berubah saat langkah mendarat — tidak menghitung mundur tiap detik — sehingga tidak
menambah satu frame pun ke rencana concat. Papan jam digambar `render.draw_clocks()` di
strip bawah papan (tinggi `CLOCK_HEIGHT` kotak, jarak `CLOCK_GAP`); `fit_size(clocks=True)`
menambah tinggi kanvas untuk strip itu. Tombol "⏱ jam" di panel Tampilan nonaktif kalau
PGN-nya tidak punya `[%clk]`.

**Logo dan nama pemain digambar di PIL, bukan `drawtext`.** `core/render.furniture_layer()`
membuat satu PNG RGBA seukuran frame berisi logo + papan nama, lalu ditimpa sekali
sebagai input ffmpeg terakhir. Alasannya: `drawtext` butuh path font di dalam string
filter, dan di Windows path itu mengandung titik dua drive plus backslash yang harus
lolos dua lapis escaping — rapuh dan sulit dilacak kalau salah. PIL juga menyamakan
kendali tipografinya dengan papan yang sudah digambar PIL.

## Deploy

`https://yt.sukaweb.my.id/chess-vids/` — **tanpa login**, pilihan pengguna, walau
siapa pun yang tahu URL-nya bisa memicu render dan memakai saldo OpenRouter. Header
`X-Robots-Tag: noindex` (include `nginx.conf_noindex` milik domain) menjaganya keluar
dari mesin pencari.

- Kode: `/home/ubuntu/asisten-konten` (clone repo ini), venv sendiri, Stockfish 18 build
  `ubuntu-x86-64-avx2` di `engines/stockfish/` — bukan avx512, sebab VM cloud bisa
  dipindah ke host yang tidak mendukungnya.
- Servis: systemd `asisten-konten`, user `ubuntu`, `Nice=10` supaya website lain di
  server yang sama tetap didahulukan. Masih `127.0.0.1:8420`, tidak pernah terbuka ke luar.
- nginx: include Hestia `nginx.conf_chessvids` + `nginx.ssl.conf_chessvids` di
  `/home/admin/conf/web/yt.sukaweb.my.id/`, jadi template domain (dan dashboard YouTube
  Analytics di `/`) tidak disentuh. `location ^~ /chess-vids/` **wajib** `^~`: tanpa itu
  regex ekstensi statis milik domain menang untuk `.mp4`/`.png`/`.mp3` dan nginx
  mencarinya di `public_html`.
- UI memakai URL relatif (`api/...`, bukan `/api/...`) — itu satu-satunya hal yang
  membuatnya jalan di bawah subpath. Jangan kembalikan ke absolut.
- Update: `git pull` di server lalu `sudo systemctl restart asisten-konten`.
- Key OpenRouter di VPS: `/home/admin/web/yt.sukaweb.my.id/private/opr.txt` (mode 640,
  di luar `public_html`), ditunjuk env `OPENROUTER_KEY_FILE` di unit systemd. Dibaca
  tiap panggilan, jadi ganti key cukup timpa file itu tanpa restart. File boleh disimpan
  dari Notepad Windows — BOM dan CRLF dibuang. Urutan: env `OPENROUTER_API_KEY` →
  file ini → `.env`.

Server 2 vCPU tanpa GPU (`pick_encoder()` jatuh ke libx264 — `libcuda.so.1` tidak ada),
dibagi dengan website produksi. Terukur dengan benchmark ffmpeg identik: decode 3,0×,
encode x264 2,4–2,6× lebih lambat dari laptop, dan 7,5× dibanding NVENC yang dipakai
laptop. Proyeksinya ~18–22 menit mesin per video vs ~5 menit lokal.

**YouTube memblokir IP server ini — tanpa cookies.** yt-dlp polos gagal di tahap metadata
dengan "Sign in to confirm you're not a bot", dan `--cookies-from-browser firefox` tidak
berlaku di sana karena tidak ada profil browser. Yang membuatnya jalan: `cookies.txt`
hasil ekspor dari browser yang login, di `/home/admin/web/yt.sukaweb.my.id/private/`
(di luar `public_html`, tidak dilayani web), ditunjuk lewat env `YT_COOKIES` di unit
systemd. `video.cookie_file()` mencobanya paling awal. Terukur 2026-09-21: video 808 detik
terunduh penuh 1920x1080, 272 MB, dalam 74 detik.

- yt-dlp **menulis balik** jar cookie ke file itu setiap selesai (`ubuntu` satu grup dengan
  `admin`, jadi bisa) — termasuk **penghapusan**. Terukur 2026-09-21 20:54: sesi yang sudah
  dirotasi di browser ditolak YouTube, YouTube menyuruh menghapus cookie login, dan file
  menyusut 21 → 11 baris dengan `SID`, `SAPISID`, `LOGIN_INFO` hilang. Penyebabnya tetap
  rotasi di browser (cookie-nya sudah mati sebelum ditulis balik); tulis-balik hanya membuat
  file yang tertinggal tidak bisa dipakai untuk mendiagnosis. Cookie harus diekspor dari
  jendela incognito yang **langsung ditutup** — sesi yang masih dipakai browser akan
  dirotasi dan mematikan salinan di server dalam hitungan menit sampai jam.
- Butuh **Deno** untuk challenge JavaScript YouTube: dipasang lewat `pip install deno` ke
  venv, dan `.venv/bin` dimasukkan ke `PATH` unit systemd supaya yt-dlp menemukannya.
- Kalau cookie kedaluwarsa, error yang dilaporkan adalah error percobaan cookie, bukan
  bot-check dari percobaan terakhir — yang terakhir itu pasti gagal dan akan menyembunyikan
  sebab sebenarnya. Obatnya ekspor ulang dan unggah menimpa file yang sama.

## Struktur

```
app/main.py        FastAPI localhost, tanpa auth. Deteksi/render jalan di thread.
app/ui/index.html  satu halaman, vanilla JS, tanpa build step
core/pgn.py        parse PGN + signature okupansi 8x8 per ply + jam [%clk]
core/video.py      helper ffmpeg (unduh via yt-dlp, probe, sampling gray)
core/overlay.py    lokalisasi papan overlay + baca isi kotak
core/align.py      DP monoton frame -> ply
core/detect.py     orkestrasi deteksi overlay
core/physical.py   re-timing dari papan kayu (changepoint)
core/render.py     tema, eval bar, render concat-demuxer, overlay ke video asli,
                   furniture_layer (logo + nama pemain sebagai PNG RGBA)
core/pieces.py     rasterisasi SVG bidak lewat pycairo
core/audio.py      suara langkah dari klip di assets/sounds/ + mux
core/evaluation.py sumber evaluasi (PGN [%eval] atau engine UCI, MultiPV)
core/classify.py   nilai tiap langkah (Brilliant..Blunder) + static exchange evaluation
core/thumbnail.py  thumbnail 1280x720: frame -> OpenRouter images API, judul PIL
projects/<id>/     meta.json, input.pgn, timestamps.json, evals.json, moves.json,
                   *.mp4, thumb-source.png, thumb-ai-*.png, thumbnail.png, log.txt
```

`controller/`, `worker/`, `renderer/`, `shared/`, `tests/` adalah **sistem VPS lama
yang sudah digantikan** dan belum dihapus. Menunggu konfirmasi pengguna sebelum
dihapus lewat commit.

## Temuan terukur (jangan diturunkan ulang)

**Deteksi overlay bekerja sangat baik.** Papan overlay dicocokkan ke posisi PGN
lewat okupansi 64 kotak. Di video Carlsen–Gao, 32/32 ply dengan cost 0 (semua
kotak cocok persis). Di Murzin, 68/69 ply.

- Okupansi kotak = `std piksel > 20` — terbukti 64/64 sempurna
- Warna bidak = `fraksi piksel < 70` dengan ambang `0.269` — error 0,14% dari 5550 sampel,
  tapi hanya untuk siaran yang petak gelapnya di sekitar grey 120–145; lihat di bawah
- Rata-rata piksel **tidak bisa** dipakai untuk warna: bidak putih di kotak terang
  punya mean hampir sama dengan kotak kosong

**Ambang warna absolut patah kalau papan overlay-nya gelap.** `DARK_LEVEL = 70` diam-diam
mengandaikan petak gelap jauh lebih terang dari 70. Di Vakhidov–Carlsen (World Blitz 2023)
petak gelapnya grey **65**, jadi **75% piksel petak gelap yang kosong** sudah terhitung
"dark" — tesnya berhenti mengukur bidak dan berubah jadi mengukur petak. Akibatnya semua
bidak putih di petak gelap terbaca hitam: **657 salah-baca, 100% white-on-dark, nol di
petak terang**. Gejalanya menipu, karena geometrinya justru sempurna — okupansi cocok
persis di 231/249 frame. Yang rusak cuma warna, ~3 kotak per frame, dan itu di atas
`align.OBSERVED = 2`, sehingga mayoritas ply gagal jadi `observed` dan diinterpolasi:
**33/84 ply**. Verify gate pun nyaris menolak seluruh video (0/18, 0/18, 0/18, 5/18 —
lolos tipis di 28% vs `MIN_MATCH_RATE` 15%). Sepuluh proyek lain punya petak gelap
grey 119–144 dan tidak terpengaruh; ini bukan regresi kode, ini andaian yang akhirnya
dilanggar sebuah siaran.

`overlay.calibrate_colour()` sekarang mengambil keduanya dari videonya sendiri: `level`
di tengah dua warna petak (diukur dari petak kosong), lalu ambang putih/hitam atas
**fraksi piksel terang**, dipisah per paritas petak karena latar ikut menyumbang ke
fraksi itu.

**Ambang itu dipilih dengan dibuktikan ke PGN, bukan dengan clustering.** Ini sudah
diukur, jangan diulang: Otsu dan two-means sama-sama menaruh batas di tempat salah
begitu satu warna lebih banyak dari yang lain — keduanya membelah klaster yang **lebih
besar**, bukan celah di antaranya (19,6% sel salah baca; satu proyek jatuh dari 124/124
ke **0/124**). Valley histogram lebih baik tapi masih 2,7%. Ambang oracle di bawah 2% di
mana-mana, jadi fiturnya tidak pernah jadi masalah — memilih angkanya yang jadi masalah.
Karena petak terang dan gelap membagi papan, biaya tiap paritas cuma bergantung pada
ambangnya sendiri, jadi sapuan 47×47 cukup dua sweep murah lalu dijumlahkan (0,4 detik).

Tes lama tetap ikut dilombakan dan menang kalau memang lebih baik — itu yang membuat
perubahan ini **tidak bisa** memperburuk video yang sudah terbaca sempurna. Regresi 10
proyek (satu proyek tidak punya `input.pgn` jadi tak bisa diuji): **better=1, same=9,
worse=0**; 8 proyek memilih tes lama, 2 memakai hasil kalibrasi. Di video bermasalah
deteksi end-to-end naik **33/84 → 76/84**, keempat jendela kalibrasi jadi **18/18 (100%)**,
dan tiap ply yang teramati cost 0. Delapan ply sisanya ply terakhir setelah detik 486,5
dari video 498 detik — siarannya berhenti menampilkan papan, bukan salah baca.

Memfit ambang ke PGN **tidak** melemahkan verify gate; ini sudah diuji dengan rect yang
sengaja disalahkan: 200 px ke kiri, di atas pemain, setengah ukuran, dan pojok kiri atas
semuanya tetap **0/18** dengan median cost 26–35. Dua skalar tidak bisa mengarang posisi
catur dari grafis yang bukan papan.

**Overlay bisa digambar dari sisi hitam (terbalik 180°).** Siaran mengarahkan grafiknya ke
pemain yang sedang diikuti, jadi partai yang pemain sorotannya bermain hitam datang dalam
keadaan terputar. Terukur di Praggnanandhaa–Dubov: dibaca tegak **3/270** frame cocok,
dibaca terbalik **251/270**. Gejalanya paling menyesatkan dari semuanya — rect ketemu,
checkerboard 7,36–7,59 dari 8, papannya jelas terlihat di layar, tapi tiap ply gagal cocok
sehingga deteksi berhenti dengan "It is probably showing a different board". Padahal PGN-nya
benar dan papannya benar.

`overlay.orientation()` memutuskannya lewat **okupansi saja**, sebab itu satu-satunya sinyal
yang tidak bergantung pada ambang warna — dan ambang warna belum bisa difit sebelum
orientasinya diketahui. Posisi pembukaan simetris 180° dan tidak memberi informasi, tapi
tengah permainan memisahkan keduanya dengan telak. Di 10 proyek tegak, tegak selalu menang
dengan margin 2,5×–102× (paling sempit `405c514c171b`: 829 vs 2078).

**Rotasi dan warna harus dibetulkan berdua; satu saja tidak cukup.** Di video yang sama,
bidak hitamnya juga tidak cukup gelap untuk ambang tetap — median cuma **11%** piksel di
bawah grey 70, jadi **seluruh 3514 bidak hitam terbaca putih** (nol putih terbaca hitam).
Terukur: rotasi saja 0/76, kalibrasi warna saja 0/76, **keduanya 76/76 dengan cost 0**.
Ini kebalikan dari kasus Vakhidov–Carlsen di atas — di sana petaknya terlalu gelap, di sini
bidaknya kurang gelap — dan keduanya sama-sama membuktikan kenapa ambang absolut tidak bisa
dipertahankan.

Orientasi disimpan per layout (`overlay_flipped` / `overlay_flips` di `timestamps.json`) dan
**tidak** menyetel `flip_board`. Keduanya beda urusan: yang satu cara siaran menggambar
papannya, yang satu pilihan tampilan pengguna untuk video hasil render.

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

**Rect overlay meleset 10 px merusak seluruh video tanpa terlihat gagal.** Beda dari
kasus "papan overlay berpindah" di bawah: di sini rect-nya salah sejak awal, hasil
`refine_against_pgn()` yang menggeser tebakan `locate_geometric()` yang sebetulnya
sudah benar. Gejalanya menipu — deteksi tetap "berhasil" dan tiap ply tetap dapat
timestamp, tapi **cost-nya tidak pernah nol**: 3–5 kotak salah baca di setiap frame,
selamanya. Terukur di Carlsen–Niemann: rect `(675, 8, 584)` → hanya **14/71 ply**
`observed` (cost ≤ 2), sisanya praktis tebakan; rect yang benar `(675, 18, 584)` →
**71/71, cost 0 di semua ply**.

Dua cacat terpisah, keduanya sudah diperbaiki:

1. **Skor refine dulu memakai okupansi saja.** Papan yang dibaca meleset setengah kotak
   masih menaruh bidak di dalam hampir setiap sel, jadi okupansi nyaris tidak
   terganggu — tapi sel itu lalu berisi separuh kotak papan dan tes terang/gelap
   terbalik: bidak hitam terbaca putih. Warna adalah sinyal yang mengunci penjajaran,
   jadi skornya sekarang memakai signature penuh.
2. **Objektifnya jenuh, dan seri harus dimenangkan pergeseran terkecil.** Begitu semua
   frame informatif terbaca persis, cost mentok di nol dan seluruh dataran rect dapat
   skor identik — pemenangnya jadi urutan loop, bukan bukti. Ini menggigit dua kali:
   objektif lama ("berapa frame cocok persis") adalah fungsi tangga sehingga
   coordinate descent berhenti di dataran tempat ia mulai, dan versi pertama perbaikan
   ini membuat rect yang sudah sempurna melayang 25 px karena seri (proyek 103/103
   jatuh ke 1/103). `grid()` sekarang memakai kunci `(cost, |dx|+|dy|+|dside|)`:
   jangan bergerak tanpa bukti.

Objektifnya sendiri: jumlah cost terbaik tiap frame, di-cap `COST_CAP` supaya frame
tanpa papan tidak mendominasi, dicari lewat grid kasar→halus (`SEARCH_PASSES`).
Regresi 7 proyek setelah perbaikan: 14/71 → **71/71**, enam lainnya persis sama
(103/103, 89/89, 85/85, 74/74, 55/55, 55/56). Deteksi jadi ~40–165 detik.

**Rect dibuktikan lewat PIL, dibaca lewat ffmpeg — dan keduanya tidak sepakat soal
offset ganjil.** `refine_against_pgn()` menilai kandidat lewat `scale_window()`, yang
men-crop di NumPy dan menghormati tiap piksel; `detect()` membaca pemenangnya lewat
`sample_gray()`, yang dulu men-crop di ruang warna sumber. Filter `crop` ffmpeg
membulatkan `x` dan `y` **ke bawah** ke grid chroma, jadi di yuv420p
`crop=519:519:715:23` mengeluarkan byte yang identik persis dengan `:714:22`. Rect yang
menang di offset ganjil lalu dibaca dua piksel meleset, dan tidak ada yang melapor:
gate membuktikan satu rect, `detect()` membaca rect yang lain.

Gejalanya semenipu kasus 10 px di atas, tapi sebabnya kebalikannya — rect-nya justru
benar. Terukur di MVL–Carlsen (`3a2483633494`): rect pemenang `(715, 23, 519)` cocok
persis di **259 dari 494** frame ketika dibuktikan dan **2 dari 494** ketika dibaca,
sehingga cuma 22 dari 109 ply teramati. Dua tanda yang membedakannya dari rect yang
memang salah: cost-nya **merangkak naik** 0→5 seiring pertandingan alih-alih datar
tinggi, dan 53 ply di tengah tidak dapat timestamp sama sekali sementara ply pembuka
cost 0.

`sample_gray()` sekarang menaruh `format=gray` **sebelum** `crop`. Tanpa bidang
subsampled tidak ada yang perlu dibulatkan, dan crop menghormati koordinat apa adanya.
Regresi 22 proyek: **better=3, same=19, worse=0** — `6284f6333563` 0/97 → 96/97,
`ff2ecc3a0452` 67/124 → 124/124, MVL–Carlsen 22/109 → 109/109 (101 ply cost 0, cost
maksimum 1).

Menaruh `format=gray` di depan saja **melipatgandakan** biaya tiap panggilan, dan
`scale=iw:ih` yang mendahuluinya bukan hiasan. ffmpeg menegosiasi format **mundur** di
sepanjang rantai, dan filter yang tidak peduli format — `fps` dan `crop` termasuk —
meneruskan permintaan itu ke hulu; `scale` satu-satunya yang menyerapnya. Tanpa pin di
antara `fps` dan konversi gray, permintaannya sampai ke dekoder dan **setiap frame yang
didekode** dikonversi, bukan cuma segelintir yang disisakan `fps`. Terukur di satu
jendela refine, 18 frame keluar di ketiga versi: rantai lama 9,4 detik, tanpa pin 17,8
detik, dengan pin 8,9 detik — dan keluarannya byte-identik (sha1 sama) dengan versi
tanpa pin, di kedua rantai yang dipakai `detect()`. Deteksi end-to-end 130–142 detik
per proyek.

Kedua jalur tetap tidak identik dalam **resampling** — PIL bilinear beranti-alias vs
swscale bicubic: rect yang sama cocok persis di 259 frame lewat PIL dan 230 lewat
ffmpeg. Yang wajib sepakat cuma geometrinya, dan itu sudah. `flags=area` menaikkannya
ke 241 tapi sengaja tidak diambil; mengganti scaler menyentuh semua proyek demi
keuntungan sekecil itu.

**Jumlah kecocokan tiap jendela kalibrasi tidak sebanding antar jendela.** Merge layout
dulu memilih pemenang lewat `exact` masing-masing jendela, padahal angka itu dihitung
di frame yang berbeda-beda: jendela yang kebagian pra-pertandingan skornya rendah
sebagus apa pun rect-nya. `detect._shared_score()` sekarang menilai tiap kandidat di
gabungan frame kalibrasi **semua** jendela — frame itu sudah didekode untuk
`locate_geometric()`, jadi tidak ada panggilan ffmpeg tambahan. Di MVL–Carlsen keempat
jendela sepakat sampai 9 px dan pilihannya nyaris seri (cost 52 vs 53), jadi yang
menyelamatkan proyek itu crop-nya, bukan ini — tapi tanpa ini pemenang sebuah grup
ditentukan oleh jendela yang kebetulan paling banyak berisi permainan, bukan oleh rect
yang paling baik membaca videonya.

**Kualitas refine tergantung isi jendelanya.** Jendela kalibrasi yang banyak berisi
pra-pertandingan hanya punya sedikit frame informatif dan hasil refine-nya meleset.
Karena itu `calibrate()` me-refine **setiap** jendela (tidak lagi melewati jendela yang
rect awalnya sama), lalu menggabungkan layout berdekatan (`SAME_RECT`, 24 px) dan
memilih pemenangnya lewat `_shared_score()` — lihat di atas kenapa bukan lewat angka
per jendela. Di Carlsen–Niemann jendela pertama (40% isinya sebelum permainan mulai)
terbukti 9/18 sementara jendela terakhir 17/18.

**Overlay kadang menampilkan papan lain.** Di turnamen beregu grafisnya bergiliran
antar papan. Sekitar 4 dari 18 frame kalibrasi tidak cocok. Karena itu ada verify
gate: sumber apa pun harus membuktikan diri terhadap PGN sebelum dipercaya.

**Papan overlay BERPINDAH di tengah siaran — satu rect untuk seluruh video tidak
cukup.** Ini penyebab paling sering ply-ply awal kosong, dan gejalanya menipu: mirip
"overlay menampilkan papan lain", padahal papannya ada dan benar, cuma bergeser.
Terukur di video nyata: Niemann–Gukesh `(674,16,520)` → `(618,16,520)` (geser 56 px,
= hampir satu kotak) dan Carlsen–Sindarov `(602,8,528)` → `(569,9,560)` (geser sekaligus
ganti ukuran). Di bawah rect yang salah, crop-nya meleset dan seluruh 64 kotak terbaca
acak: median cost 26 vs 1, frame informatif 9/200 vs 150/200. Akibatnya 29 ply pembuka
hilang total meski overlay-nya kelihatan jelas di layar.

Sebabnya `locate_geometric()` me-median semua frame yang diberikan, jadi satu panggilan
global hanya menemukan layout yang mendominasi timeline; layout minoritas terhapus.
`detect.calibrate()` sekarang mencari per jendela waktu (`CALIBRATION_WINDOWS`), tiap
kandidat dibuktikan ke PGN **di jendelanya sendiri** (kalau dibuktikan ke seluruh video,
layout yang cuma benar 20% waktu akan tampak gagal), lalu `detect()` men-sampling video
sekali per layout dan mengambil **cost minimum per (frame, ply)**. Rect yang salah tidak
membingkai papan sama sekali sehingga tidak pernah cocok dengan posisi mana pun — jadi
minimum itu selalu memilih bacaan yang benar tanpa perlu tahu di detik berapa siarannya
berpindah. Hasil terukur: 55/84 → **84/84 ply**, nol ply kosong; proyek yang tadinya
sudah bagus tidak berubah (108/108 dan 98/99 tetap sama), dan kandidat sampah
(checkerboard 2.36/8) tetap ditolak gate di 0/18.

**Bidak yang sudah tergantung sejak sebelum langkah bukan pengorbanan.** Ini kesalahan
paling produktif dalam mencetak "brilliant" palsu, dan gejalanya meyakinkan: SEE benar,
bidaknya memang bisa dimakan, engine memang menyukai langkahnya. Terukur di
Carlsen–Firouzja: 3 dari 4 brilliant (ply 45 Qd7, 57 g3, 59 Kg2) adalah langkah tenang di
sebelah kuda yang sudah berdiri en prise beberapa langkah — taboo karena memakannya kena
taktik — sehingga **setiap** langkah tenang berikutnya terbaca sebagai pengorbanan bidak
baru. Di Gukesh–Firouzja ply 61 lebih telak lagi: `Rxd7` yang justru **menang** benteng
dinilai sac=5,0. `sacrifice_value()` sekarang mengukur selisih: apa yang sudah bisa
dimenangkan lawan sebelum langkah (diukur lewat null move) dikurangkan dari yang bisa
dimenangkannya sesudah. Keempat kasus di atas jatuh ke nol.

**Rekapture bukan "great".** Selisih ke PV kedua lebar di rekapture karena alternatifnya
memang membuang bidak, bukan karena langkahnya istimewa. Sebelum dikecualikan: 9 great di
109 ply (fxe6, Rxa8, axb5, Qxc6 …); sesudah: 5. Langkah yang cuma satu-satunya legal juga
dibuang — di Caruana–Carlsen ply 57 `Qf1` mengorbankan menteri (sac 4,0, rugi 0,6 poin)
tetapi itu satu-satunya cara keluar dari skak.

**Ambang "sudah menang telak" (`WON`) dipatok ke chess.com, bukan dikira-kira.** Mula-mula
75% (+3,0), dan itu terlalu sempit: di Aravindh–Dubov ply 74, `37...Rxe5` (korban kualitas,
sac 1,75, rugi 0,0 poin) ditolak semata-mata karena hitam sudah +4,10 — padahal chess.com
menyebutnya brilliant. Sekarang 90% (+6,0). Yang masih ditolak adalah pengorbanan di posisi
yang memang sudah selesai: `Bd4+` di +10. Sebarannya di 13 proyek: 75% → 3 brilliant,
85% → 6, 90% → **7**, 95% → 8.

**Sebarannya sekarang.** 13 proyek, 1256 ply partai elit: 1021 excellent, 194 good,
21 inaccuracy, 9 mistake, **7 brilliant**, **4 blunder**. Brilliant memang harus langka;
kalau satu perubahan ambang membuatnya belasan, yang berubah bukan mutu deteksinya.
(Angka ini dari jalur `[%eval]`/skor saja, jadi tanpa `best` dan `great` — keduanya butuh
langkah terbaik versi engine.)

## Engine

Stockfish 18 (build `bmi2`, cocok untuk Intel Kaby Lake) terpasang di
`engines/stockfish/stockfish-windows-x86-64-bmi2.exe` dan terdeteksi otomatis
oleh `evaluation.find_engine()`. Folder `engines/` di-gitignore — kalau repo
di-clone ulang, unduh lagi dari rilis resmi `official-stockfish/Stockfish`.

`evaluation.analyse_game()` menganalisis **posisi**, bukan langkah: N+1 entri, indeks 0
adalah posisi awal. Nilai untuk langkah 1 butuh posisi sebelum putih melangkah, dan tidak
ada PGN yang menuliskannya. Tiap entri membawa skor sudut pandang putih, langkah terbaik
menurut engine, dan skor **PV kedua** (`multipv=2`) — selisih keduanya yang membedakan
"cuma satu langkah yang jalan" dari "banyak pilihan enak", dan harganya satu PV tambahan,
bukan satu lintasan analisis kedua.

Evaluasi 109 ply memakan ~23 detik pada `movetime=0.2` dengan MultiPV 2 dan 3 thread.

## Jebakan yang sudah pernah menggigit

- **Freestyle/Chess960 harus dibawa sampai ke engine.** PGN Freestyle punya header
  `[Variant "Chess960"]` + `[FEN ...]`, dan rokade ditulis raja-makan-benteng (`d1g1`).
  Papan yang dibangun tanpa `chess960=True` mengirim posisi yang tidak bisa diurai
  Stockfish: engine menjawab langkah ilegal, lalu berhenti menjawab sama sekali, dan
  python-chess melempar `TimeoutError` **tanpa pesan** setelah 10 detik. Terukur di
  Carlsen–Niemann: gagal persis di ply 19 (O-O). Karena itu `pgn.parse_pgn()` menyimpan
  flag `chess960` dan semua rekonstruksi papan lewat `pgn.board_from()`. Jangan set
  opsi `UCI_Chess960` sendiri — python-chess mengurusnya dari flag papan dan menolak
  kalau diset manual. Signature okupansi tidak terpengaruh (posisi hasil rokade sama
  saja), jadi deteksi proyek 960 lama tetap benar.
- **Kotak rokade di render tidak boleh ditabelkan.** `render.castling_squares()`
  menghitungnya dari posisi, sebab di 960 raja dan benteng mulai dari petak lain;
  tujuannya tetap g/f (kingside) dan c/d (queenside) di kedua varian.
- **Badge nilai langkah butuh dua font, dan stroke-nya tidak boleh tebal.** Segoe UI
  Bold (`find_text_font()`) tidak punya glyph bintang sama sekali — `best` keluar sebagai
  kotak kosong; Segoe UI Symbol (`find_font()`) punya bintangnya tapi cuma satu berat.
  Karena itu `badge_font()` memilih per simbol: tanda baca ke muka tebal, sisanya ke muka
  simbol. Penebalan lewat `stroke_width` juga ada batasnya: di `BADGE_STROKE` 0,024 ke atas
  celah di bawah tanda seru tertutup dan `!` berubah jadi balok putih polos, dan tanpa
  `BADGE_TRACKING` kedua tanda di `!!` / `??` menyatu jadi satu gumpalan.
- **Exception bisa tidak punya pesan.** `str(error)` untuk `TimeoutError` di atas
  kosong, jadi UI cuma menampilkan "failed" tanpa sebab. `background()` sekarang jatuh
  ke nama kelasnya kalau pesannya kosong.
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

- **Hapus sistem VPS lama** setelah pengguna puas dengan yang baru.
- **Test otomatis untuk `core/`** belum ada. `tests/` yang ada menguji sistem lama.
- Pemutaran video di panel review belum pernah diverifikasi di browser sungguhan
  (panel headless tidak meng-compose frame sehingga video ter-suspend).

## Bahasa

Pengguna berkomunikasi dalam bahasa Indonesia. Balas dalam bahasa Indonesia.
Komentar dan nama di dalam kode tetap bahasa Inggris.
