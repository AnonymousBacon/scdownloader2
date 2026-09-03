import re
from typing import Any, Dict, List, Optional

import requests
from loguru import logger

SOUNDCLOUD_APP_URL = "https://soundcloud.com"
API_V2_BASE = "https://api-v2.soundcloud.com"


class SoundCloudAPIError(Exception):
    pass


class SoundCloudAPI:
    """
    Minimal client for SoundCloud's current public web API (api-v2.soundcloud.com).

    SoundCloud has no published developer API for this use case anymore, so
    every unofficial tool authenticates the same way the soundcloud.com web
    player does: with a `client_id` that is embedded in the player's bundled
    JavaScript. This client discovers that client_id by scraping it fresh
    from soundcloud.com, so it keeps working as SoundCloud rotates the id.
    """

    def __init__(self, proxy: Optional[str] = None, client_id: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            }
        )
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self.client_id = client_id or self._fetch_client_id()

    def _fetch_client_id(self) -> str:
        logger.debug("Discovering current SoundCloud client_id from soundcloud.com")
        resp = self.session.get(SOUNDCLOUD_APP_URL, timeout=15)
        resp.raise_for_status()

        script_urls = re.findall(r'<script[^>]+src="([^"]+)"', resp.text)
        script_urls = [u for u in script_urls if "/assets/" in u]
        if not script_urls:
            raise SoundCloudAPIError(
                "Could not locate SoundCloud's app script bundles; "
                "the site markup may have changed."
            )

        # The client_id lives in one of the bundled JS files. Newer bundles
        # tend to come last, so search in reverse to find it faster.
        for script_url in reversed(script_urls):
            try:
                js_resp = self.session.get(script_url, timeout=15)
            except requests.RequestException:
                continue
            if js_resp.status_code != 200:
                continue
            match = re.search(r'client_id\s*:\s*"([a-zA-Z0-9]+)"', js_resp.text)
            if match:
                client_id = match.group(1)
                logger.debug(f"Discovered client_id: {client_id}")
                return client_id

        raise SoundCloudAPIError(
            "Could not extract a client_id from SoundCloud's app bundles. "
            "SoundCloud may have changed how the web player is built."
        )

    def _get(self, path: str, **params) -> Any:
        params["client_id"] = self.client_id
        resp = self.session.get(f"{API_V2_BASE}{path}", params=params, timeout=30)
        if resp.status_code == 401:
            raise SoundCloudAPIError("SoundCloud API rejected the client_id (401).")
        resp.raise_for_status()
        return resp.json()

    def resolve(self, url: str) -> Dict[str, Any]:
        """Resolve any soundcloud.com URL (track, playlist/set, user) to its API object."""
        return self._get("/resolve", url=url)

    def get_tracks(self, ids: List[int]) -> List[Dict[str, Any]]:
        """Fully hydrate a batch of track ids (playlists return stub track objects)."""
        if not ids:
            return []
        return self._get("/tracks", ids=",".join(str(i) for i in ids))

    def get_stream_url(self, transcoding: Dict[str, Any]) -> str:
        """Exchange a transcoding descriptor for a signed, playable CDN URL."""
        resp = self.session.get(
            transcoding["url"], params={"client_id": self.client_id}, timeout=30
        )
        resp.raise_for_status()
        return resp.json()["url"]
