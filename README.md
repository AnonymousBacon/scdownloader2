# scdownloader2

Download a SoundCloud track or playlist as MP3 or M4A.

Talks to SoundCloud's current web API (`api-v2.soundcloud.com`) directly —
it discovers the `client_id` the soundcloud.com web player currently uses by
scraping it live from the site, so it keeps working as SoundCloud rotates
the id — and hands the resolved stream straight to `ffmpeg`, which does the
fetching and encoding itself.

## Install

**1. Install ffmpeg** (the only non-Python dependency):

```bash
brew install ffmpeg          # macOS
sudo apt-get install ffmpeg  # Ubuntu/Debian
```

Windows: download from [ffmpeg.org](https://www.ffmpeg.org/download.html) and add it to `PATH`.

**2. Install the CLI.** [pipx](https://pipx.pypa.io) is recommended — it
installs `scdownloader2` as an isolated, globally-runnable command and pulls
in every Python dependency automatically, with nothing to manage by hand:

```bash
pipx install git+https://github.com/AnonymousBacon/scdownloader2.git
```

(No pipx? `pip install --user git+https://github.com/AnonymousBacon/scdownloader2.git` works too.)

Since this is a private repo, `git` will prompt for GitHub auth on install —
easiest is to have the [GitHub CLI](https://cli.github.com) installed and
run `gh auth login` once, which `git` then reuses automatically.

To hack on it locally instead:

```bash
git clone https://github.com/AnonymousBacon/scdownloader2.git
cd scdownloader2
pip install -e .
```

## Usage

Once installed, `scdownloader2` is on your `PATH`.

**Interactive:**

```bash
scdownloader2
```

You'll be prompted for the URL, output directory, whether to zip, and an
optional proxy.

**Flags:**

```bash
scdownloader2 --url "https://soundcloud.com/user/sets/playlist" --output downloads --zip
```

- `--url`: SoundCloud playlist or track URL
- `--output`: Output directory (default: `output`)
- `--refresh DIR`: Folder that already holds this playlist's songs — download only what's missing, straight into it
- `--proxy`: Proxy URL, e.g. `http://proxy.example.com:8080`
- `--zip`: Zip the downloaded tracks (playlists only)
- `--format mp3|m4a`: Output format (default `mp3`). `m4a` keeps SoundCloud's AAC stream untouched — see below
- `--bitrate 128|192|256|320`: MP3 bitrate (default `320`)
- `--overwrite`: Re-download tracks even if the file is already there
- `--verify`: Check existing files are complete and re-download any that were cut off

## Refreshing a playlist you already downloaded

Downloads are incremental by default: a track is skipped when a file of the
same name is already sitting in the target folder. So when tracks get added
to a playlist, you just point the tool at your existing songs and it fetches
only the new ones.

```bash
scdownloader2 --url "https://soundcloud.com/user/sets/playlist" --refresh ~/Music/my-playlist
```

`--refresh` downloads **into that exact folder** — it won't nest another
`<playlist name>/` folder inside it. Re-running the plain `--output` form
works too, since anything already downloaded there is skipped.

In interactive mode there's nothing extra to do: if the folder you enter
already contains songs, it's treated as a refresh of that folder
automatically.

Some details worth knowing:

- Tracks are matched **by filename** (`Artist - Title.mp3`, or `.m4a`). If an artist
  renames a track on SoundCloud, it no longer matches and downloads again
  under the new name.
- Downloads are written to a temporary `.part` file and renamed only once
  ffmpeg finishes, so an interrupted run never leaves a half-file that a
  later refresh would mistake for a complete download.
- For files downloaded before that safeguard existed, `--verify` checks each
  one against the length SoundCloud reports and re-downloads any that were
  cut off. It decodes rather than trusting the MP3 header, because a
  truncated MP3 still advertises the original duration.
- `--zip` can't be combined with `--refresh`: zipping replaces the folder
  with a `.zip`, which would remove the songs already in it.

## Audio quality

The quality ceiling is set by what SoundCloud streams, not by the output
bitrate. For free streams the best source is AAC at 160 kbps (`aac_160k`,
the same stream SoundCloud's own web player uses), and that's what this
tool picks. The alternative, `mp3_1_0`, is MP3 at 128 kbps. Both top out at
around 16 kHz. Higher-quality sources exist but aren't reachable here: the
256 kbps stream requires a paid Go+ subscription, and the uploader's
original file requires a login plus the uploader having enabled downloads.

So no setting produces "true" 320 kbps audio: the information isn't in the
stream. What the output settings control is how much *extra* loss the
conversion adds:

- **`--format mp3` (default, 320 kbps):** re-encodes the AAC stream to MP3.
  A lossy-to-lossy conversion always costs a little; at 320 kbps the cost is
  small. Measured on a real track, the 320 kbps output matched its source
  about 10 dB more closely than a 192 kbps encode did (roughly a tenth of the
  added error). Files are about 2.4 MB per minute.
- **`--format m4a`:** no conversion at all. The file holds SoundCloud's AAC
  stream exactly as served (verified bit-identical), at about 1.2 MB per
  minute. This is the most faithful copy you can get. Most players and DJ
  software (rekordbox, Serato, Traktor, Apple Music) play `.m4a`.

Files downloaded by versions before 0.3.0 used the 128 kbps MP3 source
re-encoded at 192 kbps. To upgrade a folder of those, refresh it with
`--overwrite`.

## How it works

1. `GET https://api-v2.soundcloud.com/resolve?url=<url>` turns any
   soundcloud.com URL into a track or playlist object.
2. Playlists return "stub" track entries; any without a `media` block are
   hydrated in batches via `GET /tracks?ids=...`.
3. Each track lists one or more `transcodings` (AAC and MP3, over HLS or
   progressive download). The highest-bitrate one is picked, usually
   `aac_160k`. Encrypted/DRM'd (SoundCloud Go+) streams are skipped since
   they can't be decoded without a paid login.
4. The chosen transcoding's `url` is a signed API endpoint, not the media
   itself — one more `GET` (with `client_id`) returns the actual, short-lived
   CDN URL.
5. That URL is handed to `ffmpeg`, which fetches it and either encodes it to
   MP3 (320 kbps by default) or copies the AAC into an `.m4a` unchanged,
   tagging title and artist.

## Disclaimer

For educational/personal use. Respect copyright law and SoundCloud's terms
of service.
