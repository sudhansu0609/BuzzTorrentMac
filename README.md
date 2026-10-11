# ⚡ BuzzTorrent for Mac

A safety-first torrent metasearch for macOS. Search movies, TV, music, books and more across the Internet Archive
and (optionally) the torrent sites you add to Jackett, all at once. Every result gets a safety rating and a measured
video quality; BuzzTorrent downloads it itself, virus-scans it when it's done, and plays it, on the Mac or on your
Google TV.

> This is the Mac edition of [BuzzTorrent](https://github.com/sudhansu0609/BuzzTorrent) (same code, which also runs
> on Windows). Pure Python standard library: nothing to `pip install`. macOS 11 (Big Sur) or newer.

## Install (once)

1. Download this repository (green **Code** button → **Download ZIP**, or `git clone`), unzip it, and move the
   **BuzzTorrent** folder somewhere permanent, e.g. your Documents folder.
2. Right-click **`Setup BuzzTorrent (Mac).command`** → **Open** → **Open**. (Only the first time: macOS asks before
   running downloaded scripts.)
3. Answer the questions. It asks before each step and skips what's already installed:

   | Step | What | Why |
   |---|---|---|
   | 1 | [Homebrew](https://brew.sh) | installs the free tools below (asks for your Mac password) |
   | 2 | Python 3.10+ | runs BuzzTorrent |
   | 3 | aria2 | BuzzTorrent's built-in torrent downloader |
   | 4 | ClamAV + virus database | virus scan of every finished download |
   | 5 | VLC *(optional)* | plays every video format |
   | 6 | Jackett *(optional)* | more torrent sites, connected to BuzzTorrent automatically |
   | 7 | BuzzTorrent app | added to `~/Applications`, then started |

From then on, open **BuzzTorrent** from Launchpad, Spotlight or your Applications folder. It opens as its own window
(Chrome/Edge/Brave if installed, otherwise Safari) and quits a few seconds after you close the window.

| | |
|---|---|
| **BuzzTorrent** app (`~/Applications`) | Starts BuzzTorrent. Opening it again while it's running just opens another window. |
| **Start BuzzTorrent.command** | Same, from the folder (also offers the downloader + antivirus on its first run). |
| **Stop BuzzTorrent.command** | Force-stops it. Normally unnecessary. |
| **Setup BuzzTorrent (Mac).command** | Run again any time: updates the virus database, adds anything you skipped. |

Jackett on its own: `brew install jackett && brew services start jackett`, then `python3 app.py --connect-jackett`.
Developer mode with logs in the terminal: `python3 app.py [--port 8765] [--open]` · App-mode logs: `data/buzztorrent.log`.

**Mac specifics:** antivirus is ClamAV (plus BuzzTorrent's own content checks, which also catch Mac tricks such as
hidden `.app` programs, `.command` scripts and `.pkg`/`.dmg` installers inside "movie" downloads); "Open in video
player" uses VLC or QuickTime; "Start with the computer" is a login item. If macOS asks whether Python may accept
incoming connections when TV access is on, choose **Allow**.

## PC and TV: same features, two layouts

Both run on the same BuzzTorrent engine on this PC, so search, sources, safety, quality ranking, downloads,
watchlist, Continue watching and the private section are shared. The PC page (mouse) and TV page (remote) also
have the same features:

- **PC**: Continue watching, Your watchlist and the Indian classics rows on the home page; **My PC** (top bar) with a
  built-in player (quality choice, subtitles, resume, episode list); **▶ Play** and **☆ Watchlist** in a title's
  details; **🎤 voice search** in the search boxes (uses the language you pick). Formats the PC window can't play
  (often MKV/HEVC) open with **Open in video player** (VLC, if installed, or Windows' video app).
- **TV**: a **Downloads** screen with the PC's downloads (progress, pause, resume, stop) plus anything saved on the
  TV, and **quality / Safe only** filters on search results.
- Settings, sources, Jackett and the download scanner stay PC-only, so nobody on the Wi-Fi can change them.

## Built-in downloader

Press **⬇ Download** on any result (PC, or *Download on PC* from the TV/phone) and BuzzTorrent downloads it itself; no
other torrent app needed. Progress, speed, peers and time left are under the **Downloads** button (top right), with
pause, resume and stop. The engine is [aria2](https://aria2.github.io) (open source; installed with
`winget install aria2.aria2`), run hidden on 127.0.0.1 with a random secret and stopped when BuzzTorrent exits;
unfinished downloads resume after a restart.

- Saves into the folder the **download scanner** watches (if the scanner isn't set up, the first download turns it on
  for `Downloads\BuzzTorrent`), so every finished download gets the antivirus + content scan and then shows up in
  **My PC** on the TV.
- Risky results need a confirmation; a .torrent is only used once its info-hash is verified.
- After finishing it shares back until ratio 1.0 or 30 minutes (`downloader.seed_ratio` / `seed_minutes` in
  config.json; set `seed_minutes` to 0 to stop right away).
- Torrent sites come from **Jackett** (Settings → Sources), where you pick the sites once; every search then covers
  all of them plus the Internet Archive.

## Private 18+ section

Off and invisible by default. Settings → **Private 18+ section**: choose a PIN, turn it on, Save; then
**Open private section** with the PIN. Adult results (Torznab category 6000, plus titles that are clearly adult)
**never appear in normal searches**, on any device. While open, a 🔒 bar shows; **Lock** closes it, and it also locks
itself after 30 idle minutes or when BuzzTorrent closes (the unlock lives only in memory).

Nothing is kept: private searches don't go into browser history or the window title, aren't sent to Wikipedia,
and no search words are written to the log (for any search). Private titles can't go on the watchlist, never show
in Continue watching, and private downloads are listed as "Private download" while locked. On the TV the section
exists only if **Also on the TV** is on; it's a PIN-locked 🔒 18+ tab there. The phone remote can't reach it.

## Video quality (4K / 8K)

Every movie/TV result shows its quality (8K, 4K, 1080p, 720p, 480p, Low). For Internet Archive copies it is
**measured** from the actual video files, so an upload titled "in 4K" that is really 854×382 shows as 480p; for other
sources it comes from the release name (2160p, 1080p, DVDRip…). Among equally good matches the sharper copy ranks
first, and every movie/TV search also asks each source for "2160p", "4K" and "8K" copies. On the PC: the **Quality**
filter (720p+ / 1080p+ / 4K+ / 8K) and the **Best quality** sort.

## Smart search

- **Typos, missing/extra spaces, word order:** "nite of teh livng ded", "nightofthelivingdead" and "living dead night" all find *Night of the Living Dead*.
  Every result gets a similarity score (`buzz/fuzzy.py`). Near matches are labelled *close match* and ranked after exact ones.
- **Different words:** describe it instead ("zombies attack farmhouse 1968"). Wikipedia's search works out the title,
  and BuzzTorrent searches for that and says *"Showing results for …"*. *Did you mean* chips offer other candidates.
- **Suggestions while you type** under both search boxes (↑/↓, Enter).
- The Internet Archive has no fuzzy search of its own, so BuzzTorrent also fetches word-prefix candidates and ranks them locally.
  Jackett/Prowlarr results get the corrected title searched too.

## Indian languages: Hindi, Bhojpuri, Bengali, Urdu (+ Tamil, Telugu, Marathi, Punjabi)

- **Language filter** in the main search box, the results sidebar and on Browse. It uses the source's language
  metadata (Internet Archive). For Jackett/Prowlarr it adds the language word to the query ("… Hindi") and keeps
  releases whose names carry it. Results show a language tag.
- **Book genres:** *Stories · कहानियाँ*, *Poetry & shayari · कविता*, *Fiction & novels · उपन्यास*,
  *Non-fiction*, *Religion & spirituality*. They match subjects and titles in English, Hindi, Urdu and Bengali
  (kahani, afsana, golpo, ghazal, nazm, kobita, upanyas, naval…). Religious texts, which dominate popularity in
  several languages, have their own genre and are kept out of the others.
- **Type in any script:** Devanagari, Bengali and Urdu text is matched properly, and Indic text is romanized
  ("प्रेमचंद" ⇄ "premchand", "गोदान" ⇄ "godan"), so either spelling finds both. Hindi/Urdu connecting words (ki/ka/ke)
  aren't required. Spelling help comes from Hindi, Bengali or Urdu Wikipedia for queries in those scripts. Hinglish
  queries ("sholey") use English Wikipedia, which covers Indian cinema and literature well.
- **Browse extras:** *Digital Library of India* (books) and *Old Indian records* (film songs, ghazals, from the
  Society of Indian Record Collectors) collections. The home page has shelves for Hindi stories, Hindi poetry,
  Urdu shayari, Bengali novels, old Hindi film songs and Hindi movies (loaded as you scroll).
- Urdu titles and descriptions are laid out right-to-left. Details pages use Hindi/Bengali/Urdu Wikipedia when the
  title is in that script.
- Note: the Internet Archive has little **Bhojpuri** (mostly folk and film songs). Connect Jackett/Prowlarr for
  Bhojpuri films.

## Details page

Click any result or pick to see **poster, year, runtime, language, country, genres, director / writers / composer
(or author, developer, platforms, performer), cast, and a plot summary**, plus the uploader's own description, tags and licence.
It also links to Wikipedia, IMDb and Wikidata, and runs the file-list safety check.
The summary opens with a **Highlights** box of short points (Starring, Directed by / Author / Developer, year · runtime,
genre, a one-line "In short"), and cast and crew names are highlighted wherever they appear in the description.
Data comes from the source's metadata, Wikipedia (summary, CC BY-SA) and Wikidata (facts and freely licensed posters from
Wikimedia Commons). No API keys are needed.

*Privacy:* smart search and details send your search words and the titles you open to Wikipedia. Turn it off under
**Settings → Smart search & details**, and BuzzTorrent will only talk to your sources.

## Sources

| Source | What it covers | Setup |
|---|---|---|
| **Internet Archive** (built in) | Public-domain films, books, music, classic games and software. Web-seeded, so downloads work even with zero peers. | none |
| **Jackett / Prowlarr** (Torznab) | Whatever indexers *you* add in Jackett/Prowlarr. One connection covers hundreds of sites. | ⚙ **Settings → Connect Jackett / Prowlarr**: paste the Torznab URL and API key, then *Test connection* → *Save* |

You choose which indexers to add, and you're responsible for what you download.

## Browse & suggestions

- **Home page:** a row of popular picks for every category (movies, TV, games, books, music, software), each with *See all →*.
- **Browse** (top bar, or click a category tile): pages of **15 picks** with thumbnails and numbered pagination
  (← / → keys work too). Dropdowns for **Source, Category, Collection, Sort** (most popular, trending this week,
  recently added, oldest, A–Z) and **Decade**. The URL updates, so any page can be bookmarked or shared.
- Click a pick for a **detail popup**: magnet, verified `.torrent`, source page, and an automatic file-list safety scan.
- Internet Archive suggestions come from curated collections (feature films, film noir, classic TV, arcade and
  console games, Project Gutenberg, the Live Music Archive, 78 RPM records…). Broken items and anything without a
  torrent are skipped. With Jackett/Prowlarr connected you can also browse their **latest releases**.
- **Family-friendly suggestions** (Settings, on by default) keeps adult-themed titles out of suggestions. Search isn't filtered.
- Thumbnails are fetched by BuzzTorrent itself (images only, typed by their actual bytes, cached for a day), so the
  page never loads third-party content.

## Protection, in three layers

### 1. Before you click (search results)
- **Rebuilt magnets:** every magnet is rebuilt from its validated info-hash. `as=`/`xs=` parameters, trackers on private IPs and junk are dropped.
- **Redirect firewall** (`buzz/safehttp.py`): http/https only, per-source host allowlist re-checked on every hop, max 3 redirects, no requests into your LAN (SSRF guard), size, type and decompression-bomb limits.
- **Spam and fake scoring** (`buzz/safety.py`): double extensions, "password"/"codec required" lures, impossible sizes, fake seed counts, clickbait. **Dangerous results are hidden by default** and need confirming.

### 2. Before you download (Deep scan button)
Fetches only the torrent's *metadata*. It verifies that the real info-hash matches the listing (catches bait-and-switch) and flags executables, shortcuts and "password" files hidden in the file list.

### 3. After it downloads (Download scanner) 🛡
Name and size checks can be fooled. The download scanner checks **the actual files on disk**:

| Check | What it catches |
|---|---|
| **Microsoft Defender scan** (`MpCmdRun`, built into Windows; ClamAV fallback) | Known malware, archives included. Confirmed threats are auto-quarantined into Defender's own quarantine (restorable from Windows Security). |
| **Real file type from magic bytes** | `movie.mp4` that is actually a Windows program, shortcut, script, installer or HTA. The name doesn't matter. |
| **Archive inspection** (zip and RAR headers, nothing extracted) | Password-protected archives (antivirus can't see inside: the classic "pay for the password" scam), disguised programs inside archives, Office macros |
| **PDF active content** | `/JavaScript`, `/Launch`, `/EmbeddedFile` |
| **Bundling rules** | A "codec"/"player" `.exe` or script shipped alongside video files. Disk images that bypass Windows download warnings. |
| **Authenticode signatures** | Programs whose signature is **broken** (modified after signing), untrusted signers, and who published each program |
| **VirusTotal** (optional, off by default) | A second opinion from ~70 engines. **Only SHA-256 hashes** of risky files are sent, never the files. Needs a free API key. |

How it works: open the 🛡 page in the top bar, pick the folder your torrent app saves to, and click **Start watching**.
A download is scanned automatically once it's finished, meaning no partial files (`.!qB`, `.part`, …) and
nothing has changed for 90 seconds. Torrent apps pre-allocate files, so size alone can't tell.
Anything flagged gets a **Quarantine** button: files go to `quarantine/` renamed `*.blocked`, with a restore note.
The 🛡 icon shows a red badge while any download needs attention.

> ⚠️ No scanner catches everything, especially brand-new malware. Treat it as a strong safety net, not a
> guarantee: still never run programs that came inside a movie, music or book download.

## Living-room TV (Google TV / Android TV) 📺

Settings → **Living-room TV** → turn on **TV access**. BuzzTorrent restarts listening on your home Wi-Fi and
shows a **6-digit PIN** and this PC's address.

**Install the TV app** (once):
1. On the TV, install **Downloader** (by AFTVnews) from the Play Store.
2. TV Settings → Apps → Security & restrictions → Unknown sources → allow Downloader.
3. In Downloader, open `http://<PC address>:8765/BuzzTorrentTV.apk` and install.
4. Open **BuzzTorrent** from the TV's Apps row. It finds the PC by itself; enter the PIN.

(Or with ADB: `adb connect <tv-ip>` then `adb install android\build\BuzzTorrentTV.apk`.)
No app? Any TV browser works: `http://<PC address>:8765/tv`.

On the TV you get the same search (Movies, **TV Shows**, Music, every language), D-pad navigation, and per title:
- **▶ Play on TV**: free Internet Archive videos stream straight to the TV (episode list for series), **best quality
  by default**. The TV app's native player handles MKV, HEVC, 1080p and 4K with the TV's own decoder; if the TV can't
  decode a copy it falls back to the next one by itself. ▲▼ on the remote switch quality without losing your place.
- **My PC** (top bar): videos in the PC's download folder that the **download scanner rated clean** (and that really
  are videos by their first bytes) stream to the TV, with seeking. Needs the scanner turned on with your downloads
  folder. Subtitle files next to a video (`Film.en.srt`) are picked up.
- **Subtitles** (TV app 1.6+): ▲ in the player cycles through the video's subtitles: .srt/.vtt files in Internet
  Archive uploads (including the Archive's own auto captions) and next to PC downloads.
- **Continue watching**: the position is saved on the PC every 15 s, so any TV or browser resumes where you stopped
  ("Resume from 34:10" or "Start from the beginning").
- **Watchlist**: ☆ on a title (TV or phone). The PC re-checks every 6 hours and says so on the TV when a sharper copy
  or the first free copy turns up.
- **Indian classics rows** on the TV home: golden-age Hindi films, Bengali classics (Satyajit Ray & more), regional
  classics and old Indian film records. Films are limited to before 1965, i.e. public domain in India.
- **Auto-quality** (TV app 1.6+): before playing, a 5-second Wi-Fi check picks the sharpest copy the connection can
  carry; if it still keeps buffering, the player steps down a quality. ▼ picks a quality by hand.
- **Where to watch** lists services with a free tier first (YouTube "full movie" search, MX Player, WAVES, and the
  free sections of ZEE5, JioHotstar, SonyLIV and others). Paid titles still need that service's subscription.
- **🎤 Voice search** (TV app 1.5+): the 🎤 and 🎤 हिंदी buttons next to the search box (or a remote's Search key) use
  the TV's own speech recognition; "search for Sholay movie" becomes "Sholay". The remote's Assistant button is
  Google's and can't search inside BuzzTorrent.
- **📱 Phone remote**: on a phone on the same Wi-Fi, open `http://<PC address>:8765/remote` (or just the PC address)
  and pair with the same PIN. Searches typed or dictated on the phone appear on the TV; the results also show on the
  phone, where tapping one opens or plays it on the TV.
- **⬇ Download to TV** (TV app 1.4+): saves a free Internet Archive copy onto the TV, or onto a USB drive plugged into
  the TV, for offline viewing. You pick the quality; sizes are shown against the free space. Progress, offline play and
  delete are in **Downloads** (top bar). Files live in the app's own folder: an app update keeps them, uninstalling
  removes them. Torrent-only results go to a torrent app on the TV (e.g. LibreTorrent), if one is installed; those
  aren't scanned by the PC's antivirus.
- **⬇ Download on PC**: opens the magnet in the PC's torrent app (uTorrent, qBittorrent…). Risky results are refused.
- **Where to watch**: ZEE5, JioHotstar, SonyLIV, Netflix, Prime Video, YouTube, MX Player, WAVES (Doordarshan),
  aha, Sun NXT, hoichoi, Chaupal, STAGE, ShemarooMe, ETV Win, ManoramaMAX. On the TV this opens the installed app
  and asks it to search (or opens the Play Store); "Search all my apps" uses Google TV's own search. It's a
  launcher, not a source: nothing is pulled from those services, and what plays depends on your subscriptions.
  The PC's details page has the same list as web searches.

While TV access is on, closing the PC window leaves BuzzTorrent running for the TV (use *Stop BuzzTorrent* to quit),
and **Start with Windows** runs it in the background at sign-in.

Security: a device on the network can only reach the TV page and search/browse/play/send, and only after pairing with
the PIN (wrong PINs lock pairing for a growing delay; tokens are stored hashed). Settings, sources, the download
scanner and quarantine answer this PC only. Only private IP addresses are accepted as `Host`, so DNS rebinding stays
blocked.

**Pen drive instead of Downloader:** `python android\build_apk.py --usb` builds and copies `BuzzTorrentTV-<version>.apk`
to every plugged-in pen drive. On the TV, open it with a file manager app (File Commander, X-plore…) that you've
allowed under Unknown sources. A Chromecast with Google TV needs a USB-C hub with power pass-through for this.

**Updates:**
- TV screens and server changes (`static/tv.*`, `app.py`, `buzz/`): nothing to install. The TV loads its screens from
  the PC, and an open TV app reloads itself within a minute, once nothing is playing.
- TV app changes (`android/`): `python android\build_apk.py` (add `--usb` for a pen drive). Each build gets the next
  version number (`android\version.json`), so it installs over the old app with Downloader or the pen drive; pairing
  and settings are kept. Needs Android SDK build-tools + JDK 11+, no Gradle.
- Keep `android\buzztv.keystore` (back it up): Android only installs an update signed with the same key. Lose it and
  you'd have to uninstall the TV app first.

## The local app itself
- Binds to `127.0.0.1` only (unless TV access is on, see above) and rejects foreign `Host` headers (blocks DNS-rebinding)
- State-changing requests need a custom header + same origin (CSRF guard). API keys are never sent back to the browser.
- Strict Content-Security-Policy, no inline scripts. All result text is rendered with `textContent`.
- The browser never supplies URLs to fetch, and file actions are confined to the watched folder.

## Windows

The same code runs on Windows too: see [BuzzTorrent](https://github.com/sudhansu0609/BuzzTorrent) for the Windows
edition (Start BuzzTorrent.bat, Microsoft Defender, Desktop shortcuts).

## Sharing with friends

`python make_share.py` builds `dist/BuzzTorrent-<date>.zip`: the app, the TV app (APK) and a *START HERE.txt*.
It leaves out everything personal (config.json with your Jackett key and PINs, the data folder with history, Continue
watching, logs and the download list, the quarantine, and android/buzztv.keystore) and then scans the zip for those
values, refusing to finish if any slipped in. Friends unzip it and double-click *Start BuzzTorrent*, which offers to
install Python and aria2. Each friend runs their own BuzzTorrent with their own TV pairing and their own Jackett sites.

## Tests

```
python -m unittest discover tests            # 114 tests (the live antivirus one is skipped by default)
set BUZZ_AV_TEST=1 && python -m unittest discover tests   # also the live Defender test (EICAR file)
```

They cover magnet sanitizing, bencode/hash verification, spam scoring, a live local server attempting
redirect and XML attacks, Torznab parsing, the content inspector against crafted fakes (program-as-mp4,
password RAR, zip with `.pdf.exe`, PDF JavaScript, shortcut-as-txt), the watcher/quarantine flow, and the
real Microsoft Defender detecting and neutralising the EICAR test file.

## Layout

```
Setup BuzzTorrent (Mac).command / Start BuzzTorrent.command / Stop BuzzTorrent.command
app.py                  web server + JSON API
buzz/safehttp.py        hardened fetcher (redirect firewall, SSRF guard, size caps)
buzz/magnet.py          magnet validation + canonical rebuild
buzz/bencode.py         bounded .torrent parser, real info-hash
buzz/safety.py          pre-download risk scoring + file-list scan
buzz/engine.py          parallel search, dedupe, cache, deep scan
buzz/content_scan.py    downloaded-file inspection (magic bytes, zip/rar, pdf)
buzz/av.py              Microsoft Defender / ClamAV / VirusTotal / signatures
buzz/scanner.py         download-folder watcher, reports, quarantine
buzz/fuzzy.py           typo-tolerant title similarity, release-name cleaning
buzz/smart.py           Wikipedia query understanding (spelling fixes, "did you mean")
buzz/details.py         details page: Wikipedia + Wikidata + source metadata, image proxy
buzz/icon.py            draws the app icon; buzz/shortcuts.py creates the shortcuts
launcher.pyw            console-free launcher used by the shortcuts
buzz/tv.py              TV pairing, OTT "where to watch" list, playable videos, send-to-PC
buzz/providers/         archive_org.py, torznab.py (add your own: subclass Provider)
static/                 UI (index.html, app.js, style.css) and the TV UI (tv.html, tv.js, tv.css)
android/                Google TV app: MainActivity.java, setup page, build_apk.py
config.json             your settings (created on first run)
```
