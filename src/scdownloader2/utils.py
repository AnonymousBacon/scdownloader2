import os
import re
import zipfile
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from unidecode import unidecode


def validate_url(url: str) -> bool:
    """Return True if `url` has both a scheme and a network location."""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


def clean_filename(filename: str) -> str:
    """Transliterate to ASCII and strip characters invalid in filenames."""
    filename = unidecode(filename)
    filename = re.sub(r"[\s.]+(?=.*\.)", "_", filename)
    filename = re.sub(r'[<>:"/\\|?*]', "", filename)
    filename = re.sub(r"_{2,}", "_", filename)
    filename = filename.strip(". ")

    max_length = 255
    if len(filename) > max_length:
        name, ext = os.path.splitext(filename)
        filename = name[: max_length - len(ext)] + ext
    return filename


def create_zip(files: List[Path], zip_filename: Path, source_dir: Path) -> None:
    """Zip `files` (all under `source_dir`) into `zip_filename`."""
    with zipfile.ZipFile(zip_filename, "w") as zipf:
        for file in files:
            if file.exists():
                zipf.write(file, file.relative_to(source_dir))
