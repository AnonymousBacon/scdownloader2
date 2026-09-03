import argparse
import sys
from pathlib import Path

from loguru import logger

from .downloader import SoundCloudFfmpegDownloader
from .utils import validate_url


def run_noninteractive(url: str, output_dir: Path, should_zip: bool = False, proxy: str = None) -> None:
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    downloader = SoundCloudFfmpegDownloader(proxy=proxy)
    result = downloader.download(url, output_dir, max_workers=3, should_zip=should_zip)
    if result:
        logger.success(f"Downloaded: {result}")
    else:
        logger.error("Failed to download.")
        sys.exit(1)


def run_interactive() -> None:
    while True:
        url = input("Enter SoundCloud playlist or track URL: ")
        if validate_url(url):
            break
        logger.warning("Invalid URL. Please enter a valid SoundCloud URL.")

    output_dir_input = input("Enter output directory (default: output): ").strip() or "output"
    output_dir = (Path.cwd() / output_dir_input).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    while True:
        should_zip = input("Do you want to zip the downloaded files? (y/n): ").lower()
        if should_zip in ("y", "n"):
            should_zip = should_zip == "y"
            break
        logger.warning("Invalid input. Please enter 'y' for yes or 'n' for no.")

    proxy = input("Enter proxy URL (optional, press Enter to skip): ").strip() or None

    downloader = SoundCloudFfmpegDownloader(proxy=proxy)
    logger.info("Downloading now please wait...")
    result = downloader.download(url, output_dir, max_workers=3, should_zip=should_zip)
    if result:
        logger.success(f"Downloaded: {result}")
    else:
        logger.error("Failed to download.")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scdownloader2",
        description="Download a SoundCloud track or playlist as MP3 via the SoundCloud API + ffmpeg.",
    )
    parser.add_argument("--url", help="SoundCloud playlist or track URL (omit for interactive mode)")
    parser.add_argument("--output", default="output", help="Output directory (default: output)")
    parser.add_argument("--proxy", help="Proxy URL, e.g. http://proxy.example.com:8080")
    parser.add_argument("--zip", action="store_true", help="Zip the downloaded tracks (playlists only)")
    args = parser.parse_args()

    if args.url:
        run_noninteractive(args.url, Path(args.output), args.zip, args.proxy)
    else:
        run_interactive()


if __name__ == "__main__":
    main()
