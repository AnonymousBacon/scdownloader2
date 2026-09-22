import random
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .api import SoundCloudAPI
from .utils import clean_filename, create_zip

logger.remove()
logger.add(sys.stderr, level="INFO")

# Downloads are encoded at a constant 192 kbps, so a finished file's size is
# predictable from its duration. Used to spot truncated files cheaply.
BYTES_PER_SECOND_AT_192K = 192_000 / 8


@dataclass
class Track:
    id: int
    title: str
    artist: str
    permalink_url: str
    transcodings: List[Dict[str, Any]] = field(default_factory=list)
    policy: str = "ALLOW"
    duration_ms: Optional[int] = None


@dataclass
class Playlist:
    id: int
    title: str
    tracks: List[Track]


class SoundCloudFfmpegDownloader:
    """
    Downloads tracks/playlists from SoundCloud by talking to SoundCloud's
    current API (api-v2.soundcloud.com) directly and handing the resulting
    stream URL straight to `ffmpeg`, which does the fetching and the MP3
    transcoding in one shot (ffmpeg natively speaks both the plain
    progressive-mp3 URLs and the HLS/m3u8 URLs SoundCloud serves).
    """

    def __init__(self, proxy: Optional[str] = None):
        if not shutil.which("ffmpeg"):
            raise RuntimeError(
                "ffmpeg was not found on PATH. Install it first, e.g. "
                "`brew install ffmpeg` (macOS) or `sudo apt-get install ffmpeg` (Linux)."
            )
        self.proxy = proxy
        self.api = SoundCloudAPI(proxy=proxy)

    # -- metadata -----------------------------------------------------

    def _track_from_json(self, data: Optional[Dict[str, Any]]) -> Optional[Track]:
        if not data or data.get("kind") != "track":
            return None
        media = data.get("media", {})
        return Track(
            id=data["id"],
            title=data["title"],
            artist=(data.get("user") or {}).get("username", "Unknown Artist"),
            permalink_url=data["permalink_url"],
            transcodings=media.get("transcodings", []),
            policy=data.get("policy", "ALLOW"),
            duration_ms=data.get("full_duration") or data.get("duration"),
        )

    def _select_transcoding(self, track: Track) -> Optional[Dict[str, Any]]:
        candidates = [t for t in track.transcodings if not t.get("snipped")]
        if not candidates:
            candidates = track.transcodings

        # DRM'd (Go+) streams are AES-encrypted HLS that ffmpeg cannot play
        # without the decryption key SoundCloud never exposes here, so they
        # aren't real candidates at all.
        candidates = [
            t for t in candidates if "encrypted" not in t.get("format", {}).get("protocol", "")
        ]
        if not candidates:
            return None

        def is_mp3(t: Dict[str, Any]) -> bool:
            # Exact match: "audio/mpegurl" (an HLS playlist mime type) also
            # starts with "audio/mpeg", so a substring/prefix check here
            # would wrongly treat an HLS index as a plain MP3 stream.
            return t.get("format", {}).get("mime_type") == "audio/mpeg"

        def is_progressive(t: Dict[str, Any]) -> bool:
            return t.get("format", {}).get("protocol") == "progressive"

        for pred in (
            lambda t: is_progressive(t) and is_mp3(t),
            is_progressive,
            is_mp3,
            lambda t: True,
        ):
            match = next((t for t in candidates if pred(t)), None)
            if match:
                return match
        return None

    def get_playlist_info(self, playlist_url: str) -> Playlist:
        data = self.api.resolve(playlist_url)
        if data.get("kind") != "playlist":
            raise ValueError("That URL does not resolve to a SoundCloud playlist.")

        raw_tracks = data.get("tracks", [])
        full_tracks = {t["id"]: t for t in raw_tracks if "media" in t}
        stub_ids = [t["id"] for t in raw_tracks if "media" not in t]

        hydrated: Dict[int, Dict[str, Any]] = {}
        for i in range(0, len(stub_ids), 50):
            for t in self.api.get_tracks(stub_ids[i : i + 50]):
                hydrated[t["id"]] = t

        by_id = {**full_tracks, **hydrated}
        tracks = [
            self._track_from_json(by_id[t["id"]])
            for t in raw_tracks
            if t["id"] in by_id
        ]
        tracks = [t for t in tracks if t is not None]

        skipped = len(raw_tracks) - len(tracks)
        if skipped:
            logger.warning(
                f"Skipped {skipped} track(s) that could not be resolved "
                "(private, deleted, or otherwise unavailable)."
            )

        return Playlist(id=data["id"], title=data["title"], tracks=tracks)

    # -- "do we already have it?" ----------------------------------------

    def _expected_filename(self, track: Track) -> str:
        """The filename a track downloads to. Single source of truth, so the
        skip-if-present check can never drift from what downloads actually write."""
        return clean_filename(f"{track.artist} - {track.title}") + ".mp3"

    def _decoded_duration(self, path: Path) -> Optional[float]:
        """How much audio `path` actually contains, in seconds.

        Deliberately decodes the file instead of reading its header: a
        truncated MP3 still advertises the *original* duration in its header,
        so only decoding reveals that the audio was cut short.
        """
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-f", "null", "-"],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        matches = re.findall(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", result.stderr or "")
        if not matches:
            return None
        hours, minutes, seconds = matches[-1]
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def _file_is_complete(self, path: Path, track: Track) -> bool:
        """True if `path` looks like a complete copy of `track`."""
        if not track.duration_ms:
            return True  # nothing to compare against; assume it's fine
        expected_seconds = track.duration_ms / 1000

        # Cheap screen first: a cut-off file is dramatically smaller than a
        # whole one, and this costs nothing for the files that are fine.
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size >= 0.85 * expected_seconds * BYTES_PER_SECOND_AT_192K:
            return True

        # It looks short, but it might just have been encoded at a lower
        # bitrate elsewhere — so confirm by actually decoding it before
        # deciding to replace someone's file.
        actual = self._decoded_duration(path)
        if actual is None:
            return False  # unreadable/corrupt
        return abs(actual - expected_seconds) <= 2.0

    def _existing_filenames(self, directory: Path) -> set:
        """Names of non-empty files already in `directory`."""
        if not directory.exists():
            return set()
        return {
            p.name
            for p in directory.iterdir()
            if p.is_file() and p.stat().st_size > 0
        }

    # -- downloading ----------------------------------------------------

    def _ffmpeg_command(self, stream_url: str, track: Track, output_path: Path) -> List[str]:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if self.proxy:
            cmd += ["-http_proxy", self.proxy]
        cmd += [
            "-user_agent", self.api.session.headers["User-Agent"],
            "-headers", "Referer: https://soundcloud.com/\r\n",
            "-i", stream_url,
            "-vn",
            "-map_metadata", "-1",
            "-acodec", "libmp3lame",
            "-b:a", "192k",
            "-metadata", f"title={track.title}",
            "-metadata", f"artist={track.artist}",
            # Explicit, because downloads are written to a ".part" file whose
            # extension ffmpeg can't infer a format from.
            "-f", "mp3",
            str(output_path),
        ]
        return cmd

    def download_track(self, track: Track, output_dir: Path) -> Optional[Path]:
        if track.policy == "BLOCK":
            logger.warning(f"Skipping region-blocked track: {track.title}")
            return None

        transcoding = self._select_transcoding(track)
        if transcoding is None:
            logger.warning(f"No playable stream for '{track.title}'; skipping.")
            return None

        try:
            stream_url = self.api.get_stream_url(transcoding)
        except Exception as e:
            logger.error(f"Could not resolve stream for '{track.title}': {e}")
            return None

        output_path = Path(output_dir) / self._expected_filename(track)
        # Download to a scratch file and rename only on success, so an
        # interrupted or failed run never leaves a truncated file sitting
        # there looking like a finished download.
        part_path = output_path.parent / (output_path.name + ".part")
        cmd = self._ffmpeg_command(stream_url, track, part_path)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            logger.error(f"Timed out downloading '{track.title}'")
            part_path.unlink(missing_ok=True)
            return None

        if result.returncode != 0 or not part_path.exists():
            stderr_tail = result.stderr.strip()[-500:] if result.stderr else "no output"
            logger.error(f"ffmpeg failed for '{track.title}': {stderr_tail}")
            part_path.unlink(missing_ok=True)
            return None

        try:
            part_path.replace(output_path)
        except OSError as e:
            logger.error(f"Could not finalize '{track.title}': {e}")
            part_path.unlink(missing_ok=True)
            return None

        logger.info(f"Successfully downloaded: {output_path}")
        return output_path

    def download_single_track(
        self,
        track_url: str,
        output_dir: Path,
        overwrite: bool = False,
        verify: bool = False,
    ) -> Optional[Path]:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        data = self.api.resolve(track_url)
        track = self._track_from_json(data)
        if track is None:
            logger.error("That URL does not resolve to a single SoundCloud track.")
            return None

        existing = output_dir / self._expected_filename(track)
        if not overwrite and existing.is_file() and existing.stat().st_size > 0:
            if not verify or self._file_is_complete(existing, track):
                logger.success(f"Already downloaded: {existing}")
                return existing
            logger.warning(f"Existing file looks incomplete, re-downloading: {existing.name}")

        return self.download_track(track, output_dir)

    def download_playlist(
        self,
        playlist_url: str,
        output_dir: Path,
        max_workers: int = 3,
        min_delay: int = 3,
        max_delay: int = 10,
        should_zip: bool = False,
        target_dir: Optional[Path] = None,
        overwrite: bool = False,
        verify: bool = False,
    ) -> Optional[Path]:
        """Download a playlist, skipping tracks already present on disk.

        Args:
            target_dir: "Refresh" mode — a folder that already holds this
                playlist's songs. Tracks download straight into it, with no
                `<playlist name>/` subfolder created. When omitted, the usual
                `output_dir/<playlist name>/` layout is used.
            overwrite: Re-download tracks even when the file already exists.
            verify: Check existing files against SoundCloud's reported track
                length and re-download any that are truncated.
        """
        if should_zip and target_dir is not None:
            raise ValueError(
                "Refusing to zip while refreshing an existing folder: zipping "
                "replaces the folder with a .zip, which would remove the songs "
                "already in it."
            )

        if target_dir is not None:
            playlist_dir = Path(target_dir).resolve()
            if not playlist_dir.exists():
                logger.warning(
                    f"Refresh folder does not exist yet: {playlist_dir} — "
                    "creating it and downloading the whole playlist."
                )
            playlist_dir.mkdir(parents=True, exist_ok=True)
        else:
            output_dir = Path(output_dir).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)

        playlist = self.get_playlist_info(playlist_url)
        playlist_name = clean_filename(playlist.title)
        if target_dir is None:
            playlist_dir = output_dir / playlist_name
            playlist_dir.mkdir(exist_ok=True)

        total_tracks = len(playlist.tracks)

        # Work out what's already on disk before downloading anything.
        existing_names = set() if overwrite else self._existing_filenames(playlist_dir)
        pending: List[Track] = []
        already_have: List[Track] = []
        for track in playlist.tracks:
            if self._expected_filename(track) not in existing_names:
                pending.append(track)
                continue
            path = playlist_dir / self._expected_filename(track)
            if verify and not self._file_is_complete(path, track):
                logger.warning(f"Existing file looks incomplete, re-downloading: {path.name}")
                pending.append(track)
            else:
                already_have.append(track)

        if already_have:
            logger.info(
                f"Found {len(already_have)} of {total_tracks} track(s) already in "
                f"{playlist_dir} — skipping those."
            )
        if not pending:
            logger.success(
                f"Already up to date: all {total_tracks} track(s) are in {playlist_dir}."
            )
            return playlist_dir

        logger.info(f"Downloading {len(pending)} new track(s)...")

        downloaded_files: List[Path] = []
        failed_tracks: List[str] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_track = {
                executor.submit(self.download_track, track, playlist_dir): track
                for track in pending
            }
            try:
                for future in as_completed(future_to_track):
                    track = future_to_track[future]
                    filepath = future.result()
                    if filepath:
                        downloaded_files.append(filepath)
                    else:
                        failed_tracks.append(track.title)
                    time.sleep(random.uniform(min_delay, max_delay))
            except KeyboardInterrupt:
                logger.warning(
                    "Interrupted — letting in-flight downloads finish, "
                    "skipping the rest..."
                )
                executor.shutdown(wait=True, cancel_futures=True)
                raise

        logger.info(
            f"Download complete: {len(downloaded_files)} new, "
            f"{len(already_have)} already had, {len(failed_tracks)} failed "
            f"(of {total_tracks} track(s))"
        )
        if failed_tracks:
            logger.warning(
                f"Skipped {len(failed_tracks)} track(s): {', '.join(failed_tracks[:5])}"
            )
            if len(failed_tracks) > 5:
                logger.warning(f"... and {len(failed_tracks) - 5} more")

        if not downloaded_files and not already_have:
            logger.error("No files were successfully downloaded.")
            return None

        if should_zip:
            # Zip everything in the folder, not just this run's downloads —
            # the folder is deleted afterwards, so anything left out would be lost.
            all_files = sorted(
                p for p in playlist_dir.iterdir()
                if p.is_file() and p.suffix.lower() == ".mp3"
            )
            zip_filename = playlist_dir.parent / f"{playlist_name}.zip"
            create_zip(all_files, zip_filename, playlist_dir)
            shutil.rmtree(playlist_dir)
            return zip_filename

        return playlist_dir

    def download(
        self,
        url: str,
        output_dir: Path,
        max_workers: int = 3,
        should_zip: bool = False,
        target_dir: Optional[Path] = None,
        overwrite: bool = False,
        verify: bool = False,
    ) -> Optional[Path]:
        """Download `url`, auto-detecting whether it's a track or a playlist."""
        kind = self.api.resolve(url).get("kind")
        if kind == "playlist":
            return self.download_playlist(
                url,
                output_dir,
                max_workers=max_workers,
                should_zip=should_zip,
                target_dir=target_dir,
                overwrite=overwrite,
                verify=verify,
            )
        if kind == "track":
            return self.download_single_track(
                url,
                target_dir if target_dir is not None else output_dir,
                overwrite=overwrite,
                verify=verify,
            )
        logger.error(f"Unsupported SoundCloud URL (resolved kind: {kind!r}).")
        return None
