"""Bounded, cached requests using the repository's existing browser headers."""
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests

from shotscrape import NBA_STATS_HEADERS

LOG = logging.getLogger(__name__)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(value, f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)


def frames(payload):
    sets = payload.get("resultSets", payload.get("resultSet"))
    if isinstance(sets, dict):
        sets = [sets]
    if not isinstance(sets, list) or not sets:
        raise ValueError("Response has no result sets")
    return {r["name"]: pd.DataFrame(r["rowSet"], columns=r["headers"]) for r in sets}


class StatsClient:
    def __init__(self, cache, host="stats.wnba.com", timeout=30, retries=3,
                 delay=1.5, offline=False, refresh=False):
        self.cache = Path(cache)
        self.host = host
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.offline = offline
        self.refresh = refresh
        self.session = requests.Session()
        self.last_request = 0.0
        self.requests = 0
        self.cache_hits = 0

    def get(self, endpoint, params, key, validator, refresh=False):
        path = self.cache / endpoint / f"{key}.json"
        if path.exists() and (self.offline or not (refresh or self.refresh)):
            try:
                payload = json.loads(path.read_text())
                validator(payload)
                self.cache_hits += 1
                return payload
            except (ValueError, KeyError, TypeError):
                if self.offline:
                    raise
                LOG.warning("Invalid cache entry; refetching %s", path)
        if self.offline:
            raise ValueError(f"Offline cache miss: {path}")
        # Preserve the working scraper's headers; adapt only host/origin/referer.
        headers = NBA_STATS_HEADERS.copy()
        site = "wnba.com" if self.host == "stats.wnba.com" else "nba.com"
        headers.update(Host=self.host, Origin=f"https://www.{site}", Referer=f"https://www.{site}/")
        url = f"https://{self.host}/stats/{endpoint}"
        assert urlparse(url).hostname == self.host
        error = None
        for attempt in range(self.retries):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.requests += 1
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()  # Reject 200 HTML responses too.
                try:
                    validator(payload)
                except (ValueError, KeyError, TypeError) as exc:
                    # Keep rejected JSON separately for diagnosis, never as a
                    # cache hit. This makes upstream schema/event issues reviewable.
                    atomic_json(self.cache / "rejected" / endpoint / f"{key}.json",
                                {"error": str(exc), "payload": payload})
                    raise
                atomic_json(path, payload)  # Never cache failures/invalid/partial data.
                return payload
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                error = exc
                LOG.warning("%s %s attempt %d/%d: %s", endpoint, key, attempt + 1, self.retries, exc)
                if attempt + 1 < self.retries:
                    time.sleep(min(2 ** attempt, 10))
        raise ValueError(f"{endpoint} {key} failed: {error}")
