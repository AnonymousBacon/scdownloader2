# scdownloader2

Download a SoundCloud track or playlist as MP3.

Talks to SoundCloud's current web API (`api-v2.soundcloud.com`) directly —
it discovers the `client_id` the soundcloud.com web player currently uses by
scraping it live from the site, so it keeps working as SoundCloud rotates
the id — and hands the resolved stream straight to `ffmpeg`, which does the
fetching and MP3 encoding itself.

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
already contains mp3s, it's treated as a refresh of that folder
automatically.

Some details worth knowing:

- Tracks are matched **by filename** (`Artist - Title.mp3`). If an artist
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

## How it works

1. `GET https://api-v2.soundcloud.com/resolve?url=<url>` turns any
   soundcloud.com URL into a track or playlist object.
2. Playlists return "stub" track entries; any without a `media` block are
   hydrated in batches via `GET /tracks?ids=...`.
3. Each track lists one or more `transcodings` (progressive MP3, HLS
   variants, ...). The best progressive MP3 is preferred, falling back to
   any progressive stream, then any MP3 stream. Encrypted/DRM'd (SoundCloud
   Go+) streams are skipped since they can't be decoded without a paid
   login.
4. The chosen transcoding's `url` is a signed API endpoint, not the media
   itself — one more `GET` (with `client_id`) returns the actual, short-lived
   CDN URL.
5. That URL is handed to `ffmpeg`, which fetches it and encodes straight to
   192kbps MP3 with title/artist ID3 tags.

## Disclaimer

For educational/personal use. Respect copyright law and SoundCloud's terms
of service.
