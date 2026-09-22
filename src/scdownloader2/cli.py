import argparse
import sys
from pathlib import Path

from loguru import logger

from .downloader import AUDIO_FORMATS, SoundCloudFfmpegDownloader
from .utils import validate_url


def _holds_songs(directory: Path) -> bool:
    """True if `directory` already has audio files sitting directly in it."""
    if not directory.is_dir():
        return False
    suffixes = {f".{fmt}" for fmt in AUDIO_FORMATS}
    return any(p.suffix.lower() in suffixes for p in directory.iterdir() if p.is_file())


def _report(result) -> None:
    if result:
        logger.success(f"Done: {result}")
    else:
        logger.error("Failed to download.")
        sys.exit(1)


def run_noninteractive(
    url: str,
    output_dir: Path,
    should_zip: bool = False,
    proxy: str = None,
    refresh_dir: Path = None,
    overwrite: bool = False,
    verify: bool = False,
    audio_format: str = "mp3",
    bitrate: int = 320,
) -> None:
    if refresh_dir is None:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    downloader = SoundCloudFfmpegDownloader(proxy=proxy, audio_format=audio_format, bitrate=bitrate)
    result = downloader.download(
        url,
        output_dir,
        max_workers=3,
        should_zip=should_zip,
        target_dir=refresh_dir,
        overwrite=overwrite,
        verify=verify,
    )
    _report(result)


def run_interactive(audio_format: str = "mp3", bitrate: int = 320) -> None:
    while True:
        url = input("Enter SoundCloud playlist or track URL: ")
        if validate_url(url):
            break
        logger.warning("Invalid URL. Please enter a valid SoundCloud URL.")

    prompt = (
        "Enter output directory, or the folder where you already downloaded "
        "this playlist (default: output): "
    )
    output_dir_input = input(prompt).strip() or "output"
    chosen_dir = (Path.cwd() / output_dir_input).expanduser().resolve()

    # If they pointed at a folder that already holds songs, treat this as a
    # refresh of that folder rather than nesting another playlist folder in it.
    refresh_dir = None
    should_zip = False
    if _holds_songs(chosen_dir):
        refresh_dir = chosen_dir
        logger.info(
            f"{chosen_dir} already contains songs — refreshing it "
            "(only tracks you don't have yet will be downloaded)."
        )
    else:
        chosen_dir.mkdir(parents=True, exist_ok=True)
        while True:
            answer = input("Do you want to zip the downloaded files? (y/n): ").lower()
            if answer in ("y", "n"):
                should_zip = answer == "y"
                break
            logger.warning("Invalid input. Please enter 'y' for yes or 'n' for no.")

    proxy = input("Enter proxy URL (optional, press Enter to skip): ").strip() or None

    downloader = SoundCloudFfmpegDownloader(proxy=proxy, audio_format=audio_format, bitrate=bitrate)
    logger.info("Downloading now please wait...")
    result = downloader.download(
        url,
        chosen_dir,
        max_workers=3,
        should_zip=should_zip,
        target_dir=refresh_dir,
    )
    if result:
        logger.success(f"Done: {result}")
    else:
        logger.error("Failed to download.")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scdownloader2",
        description="Download a SoundCloud track or playlist via the SoundCloud API + ffmpeg.",
    )
    parser.add_argument("--url", help="SoundCloud playlist or track URL (omit for interactive mode)")
    parser.add_argument("--output", default="output", help="Output directory (default: output)")
    parser.add_argument(
        "--refresh",
        metavar="DIR",
        help="Folder that already holds this playlist's songs: download only the "
        "tracks missing from it, straight into it (no playlist subfolder)",
    )
    parser.add_argument("--proxy", help="Proxy URL, e.g. http://proxy.example.com:8080")
    parser.add_argument("--zip", action="store_true", help="Zip the downloaded tracks (playlists only)")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download tracks even if the file is already there",
    )
    parser.add_argument(
        "--format",
        choices=AUDIO_FORMATS,
        default="mp3",
        help="mp3 (default) re-encodes to MP3. m4a keeps SoundCloud's AAC stream "
        "exactly as served, with no re-encoding: the most faithful copy possible",
    )
    parser.add_argument(
        "--bitrate",
        type=int,
        choices=(128, 192, 256, 320),
        default=320,
        help="MP3 bitrate in kbps (default: 320). Ignored for m4a",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Check existing files are complete (needs ffprobe) and re-download truncated ones",
    )
    args = parser.parse_args()

    refresh_dir = Path(args.refresh).expanduser().resolve() if args.refresh else None
    if refresh_dir is not None and args.zip:
        parser.error(
            "--zip cannot be combined with --refresh: zipping replaces the folder "
            "with a .zip, which would remove the songs already in it."
        )

    try:
        if args.url:
            run_noninteractive(
                args.url,
                Path(args.output),
                args.zip,
                args.proxy,
                refresh_dir=refresh_dir,
                overwrite=args.overwrite,
                verify=args.verify,
                audio_format=args.format,
                bitrate=args.bitrate,
            )
        else:
            run_interactive(audio_format=args.format, bitrate=args.bitrate)
    except KeyboardInterrupt:
        logger.warning("Cancelled.")
        sys.exit(130)


if __name__ == "__main__":
    main()
