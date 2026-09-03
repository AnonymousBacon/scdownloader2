import random
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


@dataclass
class Track:
    id: int
    title: str
    artist: str
    permalink_url: str
    transcodings: List[Dict[str, Any]] = field(default_factory=list)
    policy: str = "ALLOW"


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

        filename = clean_filename(f"{track.artist} - {track.title}") + ".mp3"
        output_path = Path(output_dir) / filename
        cmd = self._ffmpeg_command(stream_url, track, output_path)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            logger.error(f"Timed out downloading '{track.title}'")
            return None

        if result.returncode != 0 or not output_path.exists():
            stderr_tail = result.stderr.strip()[-500:] if result.stderr else "no output"
            logger.error(f"ffmpeg failed for '{track.title}': {stderr_tail}")
            return None

        logger.info(f"Successfully downloaded: {output_path}")
        return output_path

    def download_single_track(self, track_url: str, output_dir: Path) -> Optional[Path]:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        data = self.api.resolve(track_url)
        track = self._track_from_json(data)
        if track is None:
            logger.error("That URL does not resolve to a single SoundCloud track.")
            return None
        return self.download_track(track, output_dir)

    def download_playlist(
        self,
        playlist_url: str,
        output_dir: Path,
        max_workers: int = 3,
        min_delay: int = 3,
        max_delay: int = 10,
        should_zip: bool = False,
    ) -> Optional[Path]:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        playlist = self.get_playlist_info(playlist_url)
        playlist_name = clean_filename(playlist.title)
        playlist_dir = output_dir / playlist_name
        playlist_dir.mkdir(exist_ok=True)

        downloaded_files: List[Path] = []
        failed_tracks: List[str] = []
        total_tracks = len(playlist.tracks)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_track = {
                executor.submit(self.download_track, track, playlist_dir): track
                for track in playlist.tracks
            }
            for future in as_completed(future_to_track):
                track = future_to_track[future]
                filepath = future.result()
                if filepath:
                    downloaded_files.append(filepath)
                else:
                    failed_tracks.append(track.title)
                time.sleep(random.uniform(min_delay, max_delay))

        logger.info(
            f"Download complete: {len(downloaded_files)}/{total_tracks} tracks downloaded"
        )
        if failed_tracks:
            logger.warning(
                f"Skipped {len(failed_tracks)} track(s): {', '.join(failed_tracks[:5])}"
            )
            if len(failed_tracks) > 5:
                logger.warning(f"... and {len(failed_tracks) - 5} more")

        if not downloaded_files:
            logger.error("No files were successfully downloaded.")
            return None

        if should_zip:
            zip_filename = output_dir / f"{playlist_name}.zip"
            create_zip(downloaded_files, zip_filename, playlist_dir)
            for f in downloaded_files:
                f.unlink()
            shutil.rmtree(playlist_dir)
            return zip_filename

        return playlist_dir

    def download(
        self,
        url: str,
        output_dir: Path,
        max_workers: int = 3,
        should_zip: bool = False,
    ) -> Optional[Path]:
        """Download `url`, auto-detecting whether it's a track or a playlist."""
        kind = self.api.resolve(url).get("kind")
        if kind == "playlist":
            return self.download_playlist(
                url, output_dir, max_workers=max_workers, should_zip=should_zip
            )
        if kind == "track":
            return self.download_single_track(url, output_dir)
        logger.error(f"Unsupported SoundCloud URL (resolved kind: {kind!r}).")
        return None
