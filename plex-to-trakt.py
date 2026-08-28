#!/usr/bin/env python3
"""
Plex -> Trakt standalone exporter with menu, local configuration, TMDB/TVDB validation and deduplication.

Goals:
- Read Plex SQLite history for one user.
- Prefer IDs already present in Plex.
- Resolve only missing items using TMDB + TVDB.
- Auto-accept only high-confidence matches.
- Never write to Plex or Trakt.
- Resolve watch events, then keep only the most recent watch per imported media.
- Produce:
    <user>-trakt-final.json
    <user>-resolved-api.json
    <user>-review.json
    <user>-report.txt
    <user>-api-cache.json

Environment variables:
    TMDB_API_TOKEN   Preferred: TMDB v4 read access token (Bearer)
    TMDB_API_KEY     Alternative: TMDB v3 API key
    TVDB_API_KEY     Required for TVDB v4
    TVDB_PIN         Optional subscriber PIN

Example:
    export TMDB_API_TOKEN='...'
    export TVDB_API_KEY='...'
    export TVDB_PIN='...'        # only if your TVDB key needs it
    sudo -E ./plex-history-to-trakt-v2.py

This script is intentionally conservative: ambiguous results go to review.json.
"""

import json
import getpass
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PLEX_DB_RELATIVE = Path(
    "Library/Application Support/Plex Media Server/"
    "Plug-in Support/Databases/com.plexapp.plugins.library.db"
)

TMDB_BASE = "https://api.themoviedb.org/3"
TVDB_BASE = "https://api4.thetvdb.com/v4"

REQUEST_TIMEOUT = 25
REQUEST_DELAY = 0.10

# Conservative thresholds.
TITLE_EXACT = 0.995
TITLE_STRONG = 0.92
TITLE_ACCEPT_MOVIE_WITH_YEAR = 0.94
TITLE_ACCEPT_MOVIE_NO_YEAR = 0.985


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def log(msg=""):
    print(msg, flush=True)


def normalize_text(value):
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def similarity(a, b):
    a = normalize_text(a)
    b = normalize_text(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def iso8601_utc(timestamp):
    if timestamp in (None, ""):
        return "unknown"
    try:
        dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return "unknown"


def year_from_timestamp(timestamp):
    if timestamp in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(
            int(timestamp), tz=timezone.utc
        ).year
    except Exception:
        return None


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def json_write(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_json(method, url, headers=None, body=None, retries=3):
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", "plex-to-trakt-standalone/1.0")

    data = None
    if body is not None:
        headers.setdefault("Content-Type", "application/json")
        data = json.dumps(body).encode("utf-8")

    last_error = None

    for attempt in range(retries):
        req = Request(url, data=data, headers=headers, method=method)

        try:
            with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read()
                time.sleep(REQUEST_DELAY)
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))

        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(
                f"HTTP {exc.code} on {url}: {raw[:500]}"
            )

            # Retry transient API/rate-limit/server errors.
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 1.0 * (attempt + 1)
                time.sleep(wait)
                continue

            raise last_error

        except (URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise RuntimeError(f"Network error on {url}: {exc}") from exc

    raise RuntimeError(str(last_error))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class Cache:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# TMDB
# ---------------------------------------------------------------------------

class TMDBClient:
    def __init__(self, cache):
        self.cache = cache
        self.token = os.environ.get("TMDB_API_TOKEN", "").strip()
        self.api_key = os.environ.get("TMDB_API_KEY", "").strip()

        if not self.token and not self.api_key:
            raise RuntimeError(
                "TMDB credentials missing. Set TMDB_API_TOKEN or TMDB_API_KEY."
            )

    def _get(self, path, params=None):
        params = dict(params or {})
        headers = {}

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            params["api_key"] = self.api_key

        url = TMDB_BASE + path
        if params:
            url += "?" + urlencode(params)

        key = "tmdb:" + url
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        result = http_json("GET", url, headers=headers)
        self.cache.set(key, result)
        return result

    def search_movie(self, title, year=None):
        params = {
            "query": title,
            "include_adult": "false",
            "language": "fr-FR",
            "page": 1,
        }
        if year:
            params["year"] = int(year)
        return self._get("/search/movie", params).get("results", [])

    def movie_details(self, movie_id):
        return self._get(
            f"/movie/{movie_id}",
            {"language": "fr-FR", "append_to_response": "external_ids"},
        )

    def search_tv(self, title):
        return self._get(
            "/search/tv",
            {
                "query": title,
                "include_adult": "false",
                "language": "fr-FR",
                "page": 1,
            },
        ).get("results", [])

    def tv_external_ids(self, series_id):
        return self._get(f"/tv/{series_id}/external_ids")

    def episode_details(self, series_id, season, episode):
        return self._get(
            f"/tv/{series_id}/season/{season}/episode/{episode}",
            {"language": "fr-FR"},
        )

    def episode_external_ids(self, series_id, season, episode):
        return self._get(
            f"/tv/{series_id}/season/{season}/episode/{episode}/external_ids"
        )


# ---------------------------------------------------------------------------
# TVDB
# ---------------------------------------------------------------------------

class TVDBClient:
    def __init__(self, cache):
        self.cache = cache
        self.api_key = os.environ.get("TVDB_API_KEY", "").strip()
        self.pin = os.environ.get("TVDB_PIN", "").strip()

        if not self.api_key:
            raise RuntimeError("TVDB_API_KEY is missing.")

        self.token = self._login()

    def _login(self):
        cache_key = "tvdb:token"
        cached = self.cache.get(cache_key)

        # Reusing a saved token is fine; if invalid, _get() will re-login once.
        if cached:
            return cached

        payload = {"apikey": self.api_key}
        if self.pin:
            payload["pin"] = self.pin

        data = http_json("POST", TVDB_BASE + "/login", body=payload)
        token = (data.get("data") or {}).get("token")
        if not token:
            raise RuntimeError(f"TVDB login failed: {data}")

        self.cache.set(cache_key, token)
        return token

    def _fresh_login(self):
        self.cache.data.pop("tvdb:token", None)
        self.cache.save()

        payload = {"apikey": self.api_key}
        if self.pin:
            payload["pin"] = self.pin

        data = http_json("POST", TVDB_BASE + "/login", body=payload)
        token = (data.get("data") or {}).get("token")
        if not token:
            raise RuntimeError(f"TVDB login failed: {data}")

        self.token = token
        self.cache.set("tvdb:token", token)

    def _get(self, path, params=None, allow_reauth=True):
        params = dict(params or {})
        url = TVDB_BASE + path
        if params:
            url += "?" + urlencode(params)

        key = "tvdb:" + url
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            result = http_json("GET", url, headers=headers)
        except RuntimeError as exc:
            if allow_reauth and "HTTP 401" in str(exc):
                self._fresh_login()
                return self._get(path, params, allow_reauth=False)
            raise

        self.cache.set(key, result)
        return result

    def search(self, query, item_type):
        # v4 search uses query plus type=series/movie.
        data = self._get(
            "/search",
            {
                "query": query,
                "type": item_type,
                "limit": 20,
            },
        )
        return data.get("data") or []

    def search_series(self, title):
        return self.search(title, "series")

    def search_movie(self, title):
        return self.search(title, "movie")

    def series_episode(self, series_id, season, episode):
        # Official TVDB docs support season + episodeNumber filters.
        data = self._get(
            f"/series/{series_id}/episodes/default",
            {
                "season": int(season),
                "episodeNumber": int(episode),
                "page": 0,
            },
        )

        payload = data.get("data") or {}
        if isinstance(payload, dict):
            episodes = payload.get("episodes") or []
        elif isinstance(payload, list):
            episodes = payload
        else:
            episodes = []

        # Be defensive: APIs have historically returned broader sets.
        exact = []
        for ep in episodes:
            sn = safe_int(ep.get("seasonNumber") or ep.get("airedSeason"))
            en = safe_int(ep.get("number") or ep.get("airedEpisode"))
            if sn == int(season) and en == int(episode):
                exact.append(ep)

        return exact

    def episode_extended(self, episode_id):
        return (self._get(f"/episodes/{episode_id}/extended").get("data") or {})


# ---------------------------------------------------------------------------
# Plex database
# ---------------------------------------------------------------------------

def create_snapshot(source_db, workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    snapshot = workdir / "plex.db"

    log(f"Copie DB  : {source_db}")
    shutil.copy2(source_db, snapshot)

    for suffix in ("-wal", "-shm"):
        source = Path(str(source_db) + suffix)
        if source.exists():
            log(f"Copie {suffix[1:].upper():<3}: {source}")
            shutil.copy2(source, Path(str(snapshot) + suffix))

    return snapshot


def db_connect(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def find_account(conn, username):
    return conn.execute(
        """
        SELECT id, name
        FROM accounts
        WHERE LOWER(name) = LOWER(?)
        LIMIT 1
        """,
        (username,),
    ).fetchone()


def plex_external_ids(conn, metadata_id):
    rows = conn.execute(
        """
        SELECT t.tag
        FROM taggings tg
        JOIN tags t ON t.id = tg.tag_id
        WHERE tg.metadata_item_id = ?
          AND t.tag_type = 314
        """,
        (metadata_id,),
    ).fetchall()

    ids = {}

    for row in rows:
        tag = row["tag"] or ""
        if tag.startswith("imdb://"):
            value = tag[7:]
            if value.startswith("tt"):
                ids["imdb_id"] = value
        elif tag.startswith("tmdb://"):
            value = tag[7:]
            if value.isdigit():
                ids["tmdb_id"] = value
        elif tag.startswith("tvdb://"):
            value = tag[7:]
            if value.isdigit():
                ids["tvdb_id"] = value

    return ids


def best_direct_id(ids, media_type):
    # Keep the output compact and deterministic.
    if media_type == "movie":
        order = ("imdb_id", "tmdb_id")
    else:
        order = ("imdb_id", "tmdb_id", "tvdb_id")

    for key in order:
        if ids.get(key):
            return {key: ids[key]}
    return None


def load_history(conn, account_id):
    return conn.execute(
        """
        SELECT
            v.id AS view_id,
            v.guid,
            v.metadata_type,
            v.title,
            v.grandparent_title,
            v.parent_index,
            v."index" AS item_index,
            v.viewed_at,
            v.originally_available_at,
            m.id AS metadata_id,
            m.title AS metadata_title,
            m.year AS metadata_year
        FROM metadata_item_views v
        LEFT JOIN metadata_items m
          ON m.guid = v.guid
        WHERE v.account_id = ?
        ORDER BY v.viewed_at ASC, v.id ASC
        """,
        (account_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Remote ID helpers
# ---------------------------------------------------------------------------

def tvdb_result_id(result):
    for key in ("tvdb_id", "tvdbId", "id"):
        value = result.get(key)
        if value is None:
            continue

        # Search result "id" can be "series-12345" or "movie-12345".
        text = str(value)
        m = re.search(r"(\d+)$", text)
        if m:
            return int(m.group(1))
    return None


def tvdb_result_names(result):
    values = []

    for key in ("name", "name_translated", "extended_title"):
        value = result.get(key)
        if value:
            values.append(value)

    translations = result.get("translations")
    if isinstance(translations, dict):
        values.extend(v for v in translations.values() if isinstance(v, str))

    aliases = result.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if isinstance(alias, str):
                values.append(alias)
            elif isinstance(alias, dict):
                for key in ("name", "title"):
                    if alias.get(key):
                        values.append(alias[key])

    return list(dict.fromkeys(values))


def best_name_similarity(source, candidate_names):
    return max([similarity(source, x) for x in candidate_names] or [0.0])


def remote_id_from_tvdb_extended(data, source_name):
    remote_ids = data.get("remoteIds") or data.get("remote_ids") or []
    source_name = source_name.casefold()

    for item in remote_ids:
        source = str(
            item.get("sourceName")
            or item.get("source_name")
            or item.get("type")
            or ""
        ).casefold()

        if source_name in source:
            return item.get("id")

    return None


# ---------------------------------------------------------------------------
# Conservative resolver
# ---------------------------------------------------------------------------

class Resolver:
    def __init__(self, tmdb, tvdb):
        self.tmdb = tmdb
        self.tvdb = tvdb

    def resolve_episode(self, item):
        show = item["show"]
        season = item["season"]
        episode = item["episode"]
        episode_title = item.get("title") or ""

        if not show or season is None or episode is None:
            return None, self.review(
                item, "missing_series_season_or_episode"
            )

        tmdb_series = self.tmdb.search_tv(show)[:10]
        tvdb_series = self.tvdb.search_series(show)[:10]

        if not tmdb_series or not tvdb_series:
            return None, self.review(
                item,
                "series_not_found_on_both_services",
                {
                    "tmdb_candidates": self.tmdb_series_preview(tmdb_series, show),
                    "tvdb_candidates": self.tvdb_series_preview(tvdb_series, show),
                },
            )

        # Build cross-service pairs where TMDB explicitly exposes the TVDB series ID.
        pairs = []

        for tm in tmdb_series:
            tm_name_score = max(
                similarity(show, tm.get("name")),
                similarity(show, tm.get("original_name")),
            )

            # Don't even consider a badly named candidate.
            if tm_name_score < 0.75:
                continue

            try:
                ext = self.tmdb.tv_external_ids(tm["id"])
            except Exception:
                continue

            tm_tvdb = safe_int(ext.get("tvdb_id"))
            if not tm_tvdb:
                continue

            for tv in tvdb_series:
                tv_id = tvdb_result_id(tv)
                if not tv_id or tv_id != tm_tvdb:
                    continue

                tv_name_score = best_name_similarity(show, tvdb_result_names(tv))
                score = min(tm_name_score, tv_name_score)

                pairs.append({
                    "tmdb_series_id": int(tm["id"]),
                    "tvdb_series_id": int(tv_id),
                    "tmdb_name": tm.get("name"),
                    "tvdb_name": (tvdb_result_names(tv) or [""])[0],
                    "series_score": score,
                })

        # Unique cross-ID pair is the core safety gate.
        unique_pairs = {}
        for pair in pairs:
            unique_pairs[
                (pair["tmdb_series_id"], pair["tvdb_series_id"])
            ] = pair
        pairs = list(unique_pairs.values())

        if len(pairs) != 1:
            return None, self.review(
                item,
                "series_cross_validation_ambiguous",
                {
                    "cross_pairs": pairs,
                    "tmdb_candidates": self.tmdb_series_preview(tmdb_series, show),
                    "tvdb_candidates": self.tvdb_series_preview(tvdb_series, show),
                },
            )

        pair = pairs[0]

        # Same S/E from both sources.
        try:
            tm_ep = self.tmdb.episode_details(
                pair["tmdb_series_id"], season, episode
            )
            tm_ext = self.tmdb.episode_external_ids(
                pair["tmdb_series_id"], season, episode
            )
        except Exception as exc:
            return None, self.review(
                item,
                "tmdb_episode_lookup_failed",
                {"error": str(exc), "series_pair": pair},
            )

        try:
            tv_eps = self.tvdb.series_episode(
                pair["tvdb_series_id"], season, episode
            )
        except Exception as exc:
            return None, self.review(
                item,
                "tvdb_episode_lookup_failed",
                {"error": str(exc), "series_pair": pair},
            )

        if len(tv_eps) != 1:
            return None, self.review(
                item,
                "tvdb_episode_not_unique",
                {"series_pair": pair, "tvdb_episode_candidates": tv_eps},
            )

        tv_ep = tv_eps[0]
        tv_ep_id = safe_int(tv_ep.get("id"))
        tm_tvdb_ep_id = safe_int(tm_ext.get("tvdb_id"))

        tm_title = tm_ep.get("name") or ""
        tv_title = tv_ep.get("name") or ""
        title_score_tm = similarity(episode_title, tm_title)
        title_score_tv = similarity(episode_title, tv_title)

        evidence = {
            "series_pair": pair,
            "tmdb_episode_id": tm_ep.get("id"),
            "tvdb_episode_id": tv_ep_id,
            "tmdb_episode_title": tm_title,
            "tvdb_episode_title": tv_title,
            "plex_episode_title": episode_title,
            "title_score_tmdb": round(title_score_tm, 4),
            "title_score_tvdb": round(title_score_tv, 4),
            "tmdb_external_ids": tm_ext,
        }

        # Strongest possible match: TMDB's TVDB episode ID == TVDB episode ID.
        if tv_ep_id and tm_tvdb_ep_id and tv_ep_id == tm_tvdb_ep_id:
            out = {
                "type": "episode",
                "watched_at": item["watched_at"],
            }

            if tm_ext.get("imdb_id"):
                out["imdb_id"] = tm_ext["imdb_id"]
            elif tm_ep.get("id"):
                out["tmdb_id"] = str(tm_ep["id"])
            else:
                out["tvdb_id"] = str(tv_ep_id)

            return out, {
                **item,
                "resolution": "api_cross_id",
                "confidence": "high",
                "evidence": evidence,
                "output": out,
            }

        # Secondary safe path V3:
        # - the series itself is cross-validated TMDB <-> TVDB by series ID
        # - the exact same season/episode exists on both services
        # - Plex title matches TMDB title exactly after normalization
        #
        # TVDB may expose only an English episode title and TMDB sometimes
        # does not expose the TVDB episode ID. We therefore do NOT require
        # the French/English episode titles to be lexically similar.
        cross_title = similarity(tm_title, tv_title)
        if (
            pair["series_score"] >= TITLE_STRONG
            and title_score_tm >= TITLE_EXACT
        ):
            out = {
                "type": "episode",
                "watched_at": item["watched_at"],
            }

            if tm_ext.get("imdb_id"):
                out["imdb_id"] = tm_ext["imdb_id"]
            elif tm_ep.get("id"):
                out["tmdb_id"] = str(tm_ep["id"])
            elif tv_ep_id:
                out["tvdb_id"] = str(tv_ep_id)
            else:
                return None, self.review(
                    item, "episode_has_no_importable_id", evidence
                )

            evidence["cross_episode_title_score"] = round(cross_title, 4)

            return out, {
                **item,
                "resolution": "api_cross_series_season_episode_tmdb_title",
                "confidence": "high",
                "evidence": evidence,
                "output": out,
            }

        return None, self.review(
            item, "episode_cross_validation_failed", evidence
        )

    def resolve_movie(self, item):
        title = item.get("title") or ""
        year = item.get("year")

        tmdb_candidates = self.tmdb.search_movie(title, year)[:10]
        tvdb_candidates = self.tvdb.search_movie(title)[:10]

        if not tmdb_candidates:
            return None, self.review(
                item,
                "movie_not_found_tmdb",
                {"tmdb_candidates": []},
            )

        ranked = []

        for tm in tmdb_candidates:
            title_score = max(
                similarity(title, tm.get("title")),
                similarity(title, tm.get("original_title")),
            )

            release_year = None
            if tm.get("release_date"):
                try:
                    release_year = int(tm["release_date"][:4])
                except Exception:
                    pass

            year_ok = (
                year is None
                or release_year is None
                or abs(int(year) - release_year) <= 1
            )

            ranked.append({
                "tmdb": tm,
                "title_score": title_score,
                "release_year": release_year,
                "year_ok": year_ok,
            })

        ranked.sort(
            key=lambda x: (x["year_ok"], x["title_score"], x["tmdb"].get("popularity", 0)),
            reverse=True,
        )

        best = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None

        threshold = (
            TITLE_ACCEPT_MOVIE_WITH_YEAR
            if year
            else TITLE_ACCEPT_MOVIE_NO_YEAR
        )

        # Find the best TVDB title/alias before applying the final TMDB gate.
        tvdb_best = None
        tvdb_best_score = 0.0

        for tv in tvdb_candidates:
            score = best_name_similarity(title, tvdb_result_names(tv))
            if score > tvdb_best_score:
                tvdb_best = tv
                tvdb_best_score = score

        # Conservative ambiguity test.
        # V3 allows a slightly weaker TMDB lexical score ONLY when:
        # - Plex/TMDB year is coherent
        # - TVDB contains an exact normalized title/alias
        # This safely covers cases such as "douze" vs "12".
        tmdb_title_ok = best["title_score"] >= threshold
        exact_tvdb_alias = tvdb_best_score >= TITLE_EXACT
        safe_alias_bridge = (
            best["year_ok"]
            and exact_tvdb_alias
            and best["title_score"] >= 0.85
        )

        if not best["year_ok"] or not (tmdb_title_ok or safe_alias_bridge):
            return None, self.review(
                item,
                "movie_tmdb_match_too_weak",
                {
                    "tmdb_candidates": self.movie_preview(ranked),
                    "tvdb_candidates": self.tvdb_series_preview(tvdb_candidates, title),
                },
            )

        if (
            second
            and second["year_ok"]
            and abs(best["title_score"] - second["title_score"]) < 0.02
        ):
            return None, self.review(
                item,
                "movie_tmdb_match_ambiguous",
                {
                    "tmdb_candidates": self.movie_preview(ranked),
                    "tvdb_candidates": self.tvdb_series_preview(tvdb_candidates, title),
                },
            )

        tmdb_id = int(best["tmdb"]["id"])
        details = self.tmdb.movie_details(tmdb_id)
        imdb_id = (
            (details.get("external_ids") or {}).get("imdb_id")
            or details.get("imdb_id")
        )

        evidence = {
            "plex_title": title,
            "plex_year": year,
            "tmdb_id": tmdb_id,
            "tmdb_title": best["tmdb"].get("title"),
            "tmdb_original_title": best["tmdb"].get("original_title"),
            "tmdb_year": best["release_year"],
            "tmdb_title_score": round(best["title_score"], 4),
            "tmdb_imdb_id": imdb_id,
            "tvdb_best_id": tvdb_result_id(tvdb_best) if tvdb_best else None,
            "tvdb_best_names": tvdb_result_names(tvdb_best) if tvdb_best else [],
            "tvdb_title_score": round(tvdb_best_score, 4),
        }

        # For a movie, require:
        # - very strong TMDB title match
        # - year agreement when Plex has a year
        # - and either strong TVDB title corroboration, OR an IMDb ID from TMDB.
        corroborated = (
            tvdb_best_score >= TITLE_STRONG
            or bool(imdb_id)
        )

        if not corroborated:
            return None, self.review(
                item, "movie_not_cross_validated", evidence
            )

        out = {
            "type": "movie",
            "watched_at": item["watched_at"],
        }

        if imdb_id:
            out["imdb_id"] = imdb_id
        else:
            out["tmdb_id"] = str(tmdb_id)

        return out, {
            **item,
            "resolution": "api_movie_validated",
            "confidence": "high",
            "evidence": evidence,
            "output": out,
        }

    @staticmethod
    def review(item, reason, evidence=None):
        return {
            **item,
            "reason": reason,
            "confidence": "manual_review",
            "evidence": evidence or {},
        }

    @staticmethod
    def tmdb_series_preview(items, source):
        out = []
        for x in items[:10]:
            out.append({
                "id": x.get("id"),
                "name": x.get("name"),
                "original_name": x.get("original_name"),
                "first_air_date": x.get("first_air_date"),
                "score": round(
                    max(
                        similarity(source, x.get("name")),
                        similarity(source, x.get("original_name")),
                    ),
                    4,
                ),
            })
        return out

    @staticmethod
    def tvdb_series_preview(items, source):
        out = []
        for x in items[:10]:
            out.append({
                "id": tvdb_result_id(x),
                "names": tvdb_result_names(x),
                "score": round(
                    best_name_similarity(source, tvdb_result_names(x)),
                    4,
                ),
            })
        return out

    @staticmethod
    def movie_preview(ranked):
        out = []
        for r in ranked[:10]:
            tm = r["tmdb"]
            out.append({
                "id": tm.get("id"),
                "title": tm.get("title"),
                "original_title": tm.get("original_title"),
                "release_year": r["release_year"],
                "title_score": round(r["title_score"], 4),
                "year_ok": r["year_ok"],
            })
        return out


def dedupe_latest(items):
    """
    Keep only the most recent watched_at for each imported media ID.

    The Trakt import format uses one external ID per object in this exporter.
    We therefore deduplicate on (type, service, id). ISO-8601 UTC timestamps
    sort chronologically as strings.

    If the same title was resolved through different services, it will not be
    merged blindly; that is intentional to avoid collapsing two distinct works.
    """
    latest = {}
    passthrough = []

    for item in items:
        service_key = None
        service_value = None

        for key in ("trakt_id", "imdb_id", "tmdb_id", "tvdb_id"):
            if item.get(key) not in (None, ""):
                service_key = key
                service_value = str(item[key])
                break

        if not service_key:
            passthrough.append(item)
            continue

        dedupe_key = (
            item.get("type"),
            service_key,
            service_value,
        )

        current = latest.get(dedupe_key)

        if current is None:
            latest[dedupe_key] = item
            continue

        current_date = current.get("watched_at") or ""
        new_date = item.get("watched_at") or ""

        if new_date > current_date:
            latest[dedupe_key] = item

    result = list(latest.values()) + passthrough
    result.sort(key=lambda x: (x.get("watched_at") or "", x.get("type") or ""))

    removed = len(items) - len(result)
    return result, removed


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Standalone menu / local configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
CACHE_PATH = SCRIPT_DIR / "api-cache.json"
EXPORTS_DIR = SCRIPT_DIR / "exports"

DEFAULT_CONFIG = {
    "plex_db": "",
    "tmdb_api_token": "",
    "tmdb_api_key": "",
    "tvdb_api_key": "",
    "tvdb_pin": "",
}


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def pause(message="\nAppuyer sur Entrée pour continuer..."):
    try:
        input(message)
    except EOFError:
        pass


def write_private_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_config():
    config = dict(DEFAULT_CONFIG)

    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                for key in DEFAULT_CONFIG:
                    if key in stored:
                        config[key] = stored[key]
        except Exception as exc:
            print(f"ATTENTION: config.json illisible: {exc}")

    return config


def save_config(config):
    clean = {key: config.get(key, DEFAULT_CONFIG[key]) for key in DEFAULT_CONFIG}
    write_private_json(CONFIG_PATH, clean)


def secret_state(value):
    return "configuré" if str(value or "").strip() else "non configuré"


def apply_api_config(config):
    # Remove previous values first so a stale shell variable never wins.
    for key in ("TMDB_API_TOKEN", "TMDB_API_KEY", "TVDB_API_KEY", "TVDB_PIN"):
        os.environ.pop(key, None)

    if str(config.get("tmdb_api_token", "")).strip():
        os.environ["TMDB_API_TOKEN"] = str(config["tmdb_api_token"]).strip()
    elif str(config.get("tmdb_api_key", "")).strip():
        os.environ["TMDB_API_KEY"] = str(config["tmdb_api_key"]).strip()

    if str(config.get("tvdb_api_key", "")).strip():
        os.environ["TVDB_API_KEY"] = str(config["tvdb_api_key"]).strip()

    if str(config.get("tvdb_pin", "")).strip():
        os.environ["TVDB_PIN"] = str(config["tvdb_pin"]).strip()


def header(title=None):
    print("=" * 66)
    print("                    PLEX → TRAKT EXPORTER")
    print("=" * 66)
    if title:
        print(title)
        print("-" * 66)


def find_plex_candidates():
    """
    Detect common Plex database locations without assuming a specific user,
    Docker layout, distribution or home directory.
    """
    candidates = []

    configured = str(load_config().get("plex_db", "")).strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.exists():
            candidates.append(configured_path)

    home = Path.home()

    direct_candidates = [
        Path("/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"),
        home / "Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db",
        home / ".local/share/plexmediaserver/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db",
        home / "plex/config" / PLEX_DB_RELATIVE,
        home / "docker/plex/config" / PLEX_DB_RELATIVE,
        Path("/opt/plex/config") / PLEX_DB_RELATIVE,
        Path("/srv/plex/config") / PLEX_DB_RELATIVE,
        Path("/docker/plex/config") / PLEX_DB_RELATIVE,
        Path("/config") / PLEX_DB_RELATIVE,
    ]

    for candidate in direct_candidates:
        try:
            if candidate.exists():
                candidates.append(candidate)
        except OSError:
            pass

    search_roots = [
        home,
        Path("/opt"),
        Path("/srv"),
        Path("/docker"),
        Path("/mnt"),
    ]

    for root in search_roots:
        if not root.exists():
            continue

        try:
            for config_dir in root.glob("**/plex/config"):
                candidate = config_dir / PLEX_DB_RELATIVE
                if candidate.exists():
                    candidates.append(candidate)
        except (OSError, PermissionError):
            pass

    unique = []
    seen = set()

    for item in candidates:
        try:
            value = str(item.resolve())
        except OSError:
            value = str(item)

        if value not in seen:
            seen.add(value)
            unique.append(Path(value))

    return unique


def configure_plex(config):
    while True:
        clear_screen()
        header("Configuration Plex")

        current = str(config.get("plex_db", "")).strip()
        print(f"\nBase actuelle :\n{current or '(non configurée)'}")
        print(f"\nÉtat : {'OK' if current and Path(current).exists() else 'INTROUVABLE'}")
        print("\n1. Modifier le chemin")
        print("2. Détection automatique")
        print("0. Retour")

        choice = input("\nChoix : ").strip()

        if choice == "0":
            return

        if choice == "1":
            value = input("\nChemin complet de la base Plex :\n> ").strip()
            if not value:
                continue
            path = Path(value).expanduser()
            if not path.exists():
                print("\nERREUR: ce fichier n'existe pas.")
                pause()
                continue
            config["plex_db"] = str(path.resolve())
            save_config(config)
            print("\nChemin Plex enregistré.")
            pause()

        elif choice == "2":
            print("\nRecherche des bases Plex disponibles...")
            candidates = find_plex_candidates()

            if not candidates:
                print("Aucune base Plex détectée automatiquement.")
                pause()
                continue

            print()
            for i, candidate in enumerate(candidates, 1):
                print(f"{i}. {candidate}")
            print("0. Annuler")

            selected = input("\nChoix : ").strip()
            if selected == "0":
                continue
            try:
                candidate = candidates[int(selected) - 1]
            except (ValueError, IndexError):
                continue

            config["plex_db"] = str(candidate)
            save_config(config)
            print("\nBase Plex enregistrée.")
            pause()


def prompt_secret(label, current_value=""):
    state = secret_state(current_value)
    print(f"\n{label} ({state})")
    print("Laisser vide pour conserver la valeur actuelle.")
    value = getpass.getpass("> ").strip()
    return value if value else current_value


def test_tmdb(config):
    apply_api_config(config)
    cache = Cache(CACHE_PATH)
    try:
        client = TMDBClient(cache)
        # A very small, stable public lookup.
        result = client.search_movie("Fight Club", 1999)
        if not isinstance(result, list):
            raise RuntimeError("réponse TMDB inattendue")
        return True, "OK"
    except Exception as exc:
        return False, str(exc)


def test_tvdb(config):
    apply_api_config(config)

    # Force a fresh login for the configuration test. Otherwise a cached token
    # could hide a bad API key.
    cache = Cache(CACHE_PATH)
    cache.data.pop("tvdb:token", None)
    cache.save()

    try:
        client = TVDBClient(cache)
        result = client.search_series("Game of Thrones")
        if not isinstance(result, list):
            raise RuntimeError("réponse TVDB inattendue")
        return True, "OK"
    except Exception as exc:
        return False, str(exc)


def configure_apis(config):
    while True:
        clear_screen()
        header("Configuration API")
        print(f"\nTMDB token : {secret_state(config.get('tmdb_api_token'))}")
        print(f"TMDB key   : {secret_state(config.get('tmdb_api_key'))}")
        print(f"TVDB key   : {secret_state(config.get('tvdb_api_key'))}")
        print(f"TVDB PIN   : {secret_state(config.get('tvdb_pin'))}")

        print("\n1. Configurer TMDB (Bearer token recommandé)")
        print("2. Configurer TVDB")
        print("3. Tester TMDB")
        print("4. Tester TVDB")
        print("5. Effacer les identifiants API")
        print("0. Retour")

        choice = input("\nChoix : ").strip()

        if choice == "0":
            return

        if choice == "1":
            print("\nTMDB")
            print("1. Bearer / Read Access Token")
            print("2. API Key v3")
            sub = input("Choix [1] : ").strip() or "1"

            if sub == "1":
                value = prompt_secret(
                    "Token TMDB",
                    config.get("tmdb_api_token", "")
                )
                if value:
                    config["tmdb_api_token"] = value
                    config["tmdb_api_key"] = ""
            elif sub == "2":
                value = prompt_secret(
                    "API Key TMDB",
                    config.get("tmdb_api_key", "")
                )
                if value:
                    config["tmdb_api_key"] = value
                    config["tmdb_api_token"] = ""
            else:
                continue

            save_config(config)
            print("\nTest TMDB...")
            ok, message = test_tmdb(config)
            print("TMDB : OK" if ok else f"TMDB : ERREUR\n{message}")
            pause()

        elif choice == "2":
            config["tvdb_api_key"] = prompt_secret(
                "Clé API TVDB",
                config.get("tvdb_api_key", "")
            )
            print("\nPIN TVDB facultatif.")
            config["tvdb_pin"] = prompt_secret(
                "PIN TVDB",
                config.get("tvdb_pin", "")
            )
            save_config(config)
            print("\nTest TVDB...")
            ok, message = test_tvdb(config)
            print("TVDB : OK" if ok else f"TVDB : ERREUR\n{message}")
            pause()

        elif choice == "3":
            print("\nTest TMDB...")
            ok, message = test_tmdb(config)
            print("TMDB : OK" if ok else f"TMDB : ERREUR\n{message}")
            pause()

        elif choice == "4":
            print("\nTest TVDB...")
            ok, message = test_tvdb(config)
            print("TVDB : OK" if ok else f"TVDB : ERREUR\n{message}")
            pause()

        elif choice == "5":
            confirm = input(
                "\nEffacer les clés TMDB/TVDB enregistrées ? [o/N] : "
            ).strip().lower()
            if confirm in ("o", "oui", "y", "yes"):
                config["tmdb_api_token"] = ""
                config["tmdb_api_key"] = ""
                config["tvdb_api_key"] = ""
                config["tvdb_pin"] = ""
                save_config(config)
                print("\nIdentifiants effacés.")
                pause()


def list_plex_users(db_path):
    conn = db_connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                a.id,
                a.name,
                COUNT(v.id) AS history_count,
                MIN(v.viewed_at) AS first_watch,
                MAX(v.viewed_at) AS last_watch
            FROM accounts a
            LEFT JOIN metadata_item_views v
              ON v.account_id = a.id
            GROUP BY a.id, a.name
            HAVING COUNT(v.id) > 0
            ORDER BY LOWER(a.name)
            """
        ).fetchall()
        return rows
    finally:
        conn.close()


def choose_user(config):
    db_path = Path(config.get("plex_db", ""))
    if not db_path.exists():
        print("\nBase Plex introuvable. Configurez d'abord le chemin Plex.")
        pause()
        return None

    try:
        users = list_plex_users(db_path)
    except Exception as exc:
        print(f"\nImpossible de lire les utilisateurs Plex : {exc}")
        pause()
        return None

    if not users:
        print("\nAucun utilisateur avec historique trouvé.")
        pause()
        return None

    while True:
        clear_screen()
        header("Utilisateurs Plex")
        print()

        for i, user in enumerate(users, 1):
            print(
                f"{i:>2}. {user['name']} "
                f"({user['history_count']} lectures)"
            )

        print("\n 0. Retour")
        choice = input("\nUtilisateur : ").strip()

        if choice == "0":
            return None

        try:
            user = users[int(choice) - 1]
            return {
                "id": int(user["id"]),
                "name": user["name"],
                "history_count": int(user["history_count"]),
            }
        except (ValueError, IndexError):
            pass


def test_plex(config):
    db_path = Path(config.get("plex_db", ""))
    if not db_path.exists():
        return False, "base Plex introuvable"

    try:
        conn = db_connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            history = conn.execute(
                "SELECT COUNT(*) FROM metadata_item_views"
            ).fetchone()[0]
        finally:
            conn.close()
        return True, f"OK ({count} comptes, {history} lectures globales)"
    except Exception as exc:
        return False, str(exc)


def full_configuration_test(config, interactive=True):
    results = []

    ok_plex, msg_plex = test_plex(config)
    results.append(("Plex DB", ok_plex, msg_plex))

    tmdb_configured = bool(
        str(config.get("tmdb_api_token", "")).strip()
        or str(config.get("tmdb_api_key", "")).strip()
    )
    if tmdb_configured:
        ok_tmdb, msg_tmdb = test_tmdb(config)
    else:
        ok_tmdb, msg_tmdb = False, "non configuré"
    results.append(("TMDB", ok_tmdb, msg_tmdb))

    if str(config.get("tvdb_api_key", "")).strip():
        ok_tvdb, msg_tvdb = test_tvdb(config)
    else:
        ok_tvdb, msg_tvdb = False, "non configuré"
    results.append(("TVDB", ok_tvdb, msg_tvdb))

    try:
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        testfile = EXPORTS_DIR / ".write-test"
        testfile.write_text("ok", encoding="utf-8")
        testfile.unlink()
        ok_exports, msg_exports = True, "OK"
    except Exception as exc:
        ok_exports, msg_exports = False, str(exc)
    results.append(("Exports", ok_exports, msg_exports))

    if interactive:
        clear_screen()
        header("Test de configuration")
        print()
        for name, ok, message in results:
            status = "OK" if ok else "ERREUR"
            print(f"{name:<12} : {status}")
            if not ok:
                print(f"  {message}")
        print()

    return results


def create_export_snapshot(source_db, workdir):
    """
    Create a coherent SQLite snapshot using Python's online backup API.

    This avoids sequentially copying Plex's DB/WAL/SHM while Plex is writing.
    Falls back to the proven DB+WAL+SHM copy only if backup() cannot be used.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    snapshot = workdir / "plex.db"

    try:
        source = sqlite3.connect(
            f"file:{source_db}?mode=ro",
            uri=True,
            timeout=30,
        )
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination, pages=1000, sleep=0.05)
        finally:
            destination.close()
            source.close()
        return snapshot
    except Exception as exc:
        print(f"Backup SQLite indisponible ({exc}).")
        print("Utilisation de la copie DB/WAL/SHM...")
        return create_snapshot(source_db, workdir)


def run_export(config, selected_user):
    db_path = Path(config["plex_db"])
    username = selected_user["name"]

    clear_screen()
    header("Préparation de l'export")
    print(f"\nUtilisateur : {username}")
    print(f"Historique : {selected_user['history_count']} lectures")

    print("\nTests avant export...")
    results = full_configuration_test(config, interactive=False)

    all_ok = True
    for name, ok, message in results:
        print(f"  {name:<10} : {'OK' if ok else 'ERREUR'}")
        if not ok:
            all_ok = False
            print(f"    {message}")

    if not all_ok:
        print("\nExport annulé : corrigez la configuration depuis le menu.")
        pause()
        return

    confirm = input("\nContinuer ? [O/n] : ").strip().lower()
    if confirm in ("n", "non", "no"):
        return

    apply_api_config(config)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_user = re.sub(r"[^A-Za-z0-9_.-]+", "_", username)
    output_dir = EXPORTS_DIR / f"{safe_user}-{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    workdir = Path(f"/tmp/plex-to-trakt-{timestamp}")
    final_path = output_dir / "trakt.json"
    resolved_path = output_dir / "resolved-api.json"
    review_path = output_dir / "review.json"
    report_path = output_dir / "report.txt"

    print("\n[1/4] Création d'un snapshot Plex cohérent...")
    try:
        snapshot = create_export_snapshot(db_path, workdir)
        conn = db_connect(snapshot)
    except Exception as exc:
        print(f"\nERREUR snapshot Plex : {exc}")
        pause()
        return

    account = find_account(conn, username)
    if not account:
        conn.close()
        print("\nERREUR: utilisateur Plex introuvable dans le snapshot.")
        pause()
        return

    account_id = int(account["id"])
    history = load_history(conn, account_id)
    print(f"      {len(history)} lectures trouvées")

    cache = Cache(CACHE_PATH)

    try:
        resolver = Resolver(
            TMDBClient(cache),
            TVDBClient(cache),
        )
    except Exception as exc:
        conn.close()
        print(f"\nERREUR initialisation API : {exc}")
        pause()
        return

    final_items = []
    api_resolved = []
    review = []

    stats = {
        "total": 0,
        "movie": 0,
        "episode": 0,
        "unsupported": 0,
        "plex_direct": 0,
        "api_resolved": 0,
        "review": 0,
        "resolved_before_dedupe": 0,
        "duplicates_removed": 0,
        "final_unique": 0,
    }

    media_resolution_cache = {}

    print("\n[2/4] Identifiants Plex + résolution TMDB / TVDB...")

    for idx, row in enumerate(history, start=1):
        stats["total"] += 1
        metadata_type = safe_int(row["metadata_type"])

        if metadata_type == 1:
            media_type = "movie"
            stats["movie"] += 1
        elif metadata_type == 4:
            media_type = "episode"
            stats["episode"] += 1
        else:
            stats["unsupported"] += 1
            stats["review"] += 1
            review.append({
                "reason": "unsupported_plex_metadata_type",
                "plex_view_id": row["view_id"],
                "plex_guid": row["guid"],
                "metadata_type": metadata_type,
                "title": row["title"],
                "watched_at": iso8601_utc(row["viewed_at"]),
            })
            continue

        watched_at = iso8601_utc(row["viewed_at"])

        direct_ids = {}
        if row["metadata_id"] is not None:
            direct_ids = plex_external_ids(conn, row["metadata_id"])

        direct = best_direct_id(direct_ids, media_type)

        if direct:
            final_items.append({
                **direct,
                "type": media_type,
                "watched_at": watched_at,
            })
            stats["plex_direct"] += 1
            continue

        item = {
            "plex_view_id": row["view_id"],
            "plex_guid": row["guid"],
            "metadata_id": row["metadata_id"],
            "type": media_type,
            "title": row["title"],
            "watched_at": watched_at,
            "year": (
                safe_int(row["metadata_year"])
                or year_from_timestamp(row["originally_available_at"])
            ),
        }

        if media_type == "episode":
            item.update({
                "show": row["grandparent_title"],
                "season": safe_int(row["parent_index"]),
                "episode": safe_int(row["item_index"]),
            })

        if media_type == "movie":
            media_key = (
                "movie",
                row["guid"] or normalize_text(row["title"]),
                item["year"],
            )
        else:
            media_key = (
                "episode",
                normalize_text(item.get("show")),
                item.get("season"),
                item.get("episode"),
                row["guid"],
            )

        cached_resolution = media_resolution_cache.get(media_key)

        if cached_resolution is None:
            try:
                if media_type == "episode":
                    resolved, diagnostic = resolver.resolve_episode(item)
                else:
                    resolved, diagnostic = resolver.resolve_movie(item)
            except Exception as exc:
                resolved = None
                diagnostic = {
                    **item,
                    "reason": "api_exception",
                    "confidence": "manual_review",
                    "evidence": {"error": str(exc)},
                }

            media_resolution_cache[media_key] = (resolved, diagnostic)
        else:
            resolved, diagnostic = cached_resolution

        if resolved:
            out = dict(resolved)
            out["watched_at"] = watched_at
            final_items.append(out)
            stats["api_resolved"] += 1

            api_diag = dict(diagnostic)
            api_diag["plex_view_id"] = row["view_id"]
            api_diag["watched_at"] = watched_at
            api_diag["output"] = out
            api_resolved.append(api_diag)
        else:
            diag = dict(diagnostic)
            diag["plex_view_id"] = row["view_id"]
            diag["watched_at"] = watched_at
            review.append(diag)
            stats["review"] += 1

        if idx % 50 == 0 or idx == len(history):
            print(
                f"\r      {idx}/{len(history)} traitées "
                f"| Plex {stats['plex_direct']} "
                f"| API {stats['api_resolved']} "
                f"| revue {stats['review']}",
                end="",
                flush=True,
            )

    print()
    conn.close()

    print("\n[3/4] Nettoyage et dédoublonnage...")
    stats["resolved_before_dedupe"] = len(final_items)
    final_items, duplicates_removed = dedupe_latest(final_items)
    stats["duplicates_removed"] = duplicates_removed
    stats["final_unique"] = len(final_items)

    # Also make review.json easier to inspect: keep all unresolved watch events,
    # but sort them chronologically. We intentionally do not silently merge
    # unresolved items because they may not yet have a trustworthy media ID.
    review.sort(key=lambda x: x.get("watched_at") or "")

    print(f"      {stats['duplicates_removed']} doublons supprimés")
    print(f"      {stats['final_unique']} éléments uniques finaux")

    print("\n[4/4] Écriture des fichiers...")
    json_write(final_path, final_items)
    json_write(resolved_path, api_resolved)
    json_write(review_path, review)

    report = f"""Plex -> Trakt Standalone Export Report

Utilisateur Plex     : {username}
Account ID           : {account_id}

Historique total     : {stats['total']}
Films                : {stats['movie']}
Episodes             : {stats['episode']}
Types non supportés  : {stats['unsupported']}

IDs Plex directs     : {stats['plex_direct']}
Résolus par API      : {stats['api_resolved']}
À revoir             : {stats['review']}

Résolus avant dédoublonnage : {stats['resolved_before_dedupe']}
Doublons supprimés          : {stats['duplicates_removed']}
Éléments uniques finaux     : {stats['final_unique']}

JSON final Trakt     : {final_path}
Résolutions API      : {resolved_path}
Revue manuelle       : {review_path}
Cache API partagé    : {CACHE_PATH}

IMPORTANT
- Le script n'écrit ni dans Plex ni dans Trakt.
- Si un média a été vu plusieurs fois, seul le watched_at le plus récent est conservé dans trakt.json.
- Seules les résolutions haute confiance sont ajoutées à trakt.json.
"""

    report_path.write_text(report, encoding="utf-8")

    print("\nExport terminé.")
    print(f"\nDossier : {output_dir}\n")
    print("  trakt.json")
    print("  review.json")
    print("  resolved-api.json")
    print("  report.txt")
    print()
    print(
        f"Résultat : {stats['final_unique']} éléments uniques | "
        f"{stats['review']} lectures à revoir"
    )
    pause()


def first_run_setup(config):
    if CONFIG_PATH.exists():
        return

    clear_screen()
    header("Première configuration")
    print(
        "\nAucune configuration n'a été trouvée.\n"
        "Le fichier config.json sera créé dans le même dossier que le script."
    )
    pause()

    # Plex first: try automatic detection before opening the menu.
    candidates = find_plex_candidates()

    if len(candidates) == 1:
        config["plex_db"] = str(candidates[0])
        save_config(config)
        print(f"\nBase Plex détectée automatiquement :\n{candidates[0]}")
        pause()
    else:
        configure_plex(config)

    # APIs second.
    clear_screen()
    header("Configuration API")
    print(
        "\nConfigure maintenant TMDB et TVDB depuis le menu API.\n"
        "Tu pourras revenir au menu principal à tout moment."
    )
    pause()
    configure_apis(config)


def main_menu():
    config = load_config()
    first_run_setup(config)
    config = load_config()

    while True:
        clear_screen()
        header()

        plex_path = str(config.get("plex_db", "")).strip()
        plex_ok = bool(plex_path and Path(plex_path).exists())
        tmdb_ok = bool(
            str(config.get("tmdb_api_token", "")).strip()
            or str(config.get("tmdb_api_key", "")).strip()
        )
        tvdb_ok = bool(str(config.get("tvdb_api_key", "")).strip())

        print("\nConfiguration")
        print(f"  Plex DB : {'OK' if plex_ok else 'À CONFIGURER'}")
        print(f"  TMDB    : {'configuré' if tmdb_ok else 'À CONFIGURER'}")
        print(f"  TVDB    : {'configuré' if tvdb_ok else 'À CONFIGURER'}")

        print("\n1. Exporter un historique Plex")
        print("2. Configurer les API")
        print("3. Configurer le chemin Plex")
        print("4. Tester la configuration")
        print("5. Quitter")

        choice = input("\nChoix : ").strip()

        if choice == "1":
            user = choose_user(config)
            if user:
                run_export(config, user)

        elif choice == "2":
            configure_apis(config)
            config = load_config()

        elif choice == "3":
            configure_plex(config)
            config = load_config()

        elif choice == "4":
            full_configuration_test(config, interactive=True)
            pause()

        elif choice == "5":
            clear_screen()
            print("Au revoir.")
            return


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print("\n\nArrêt demandé.")
