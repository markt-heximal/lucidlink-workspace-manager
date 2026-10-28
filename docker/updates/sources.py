"""Fetch the latest release of a component from its registry.

PyPI's JSON API and Docker Hub's v2 tags API are both public and unauthenticated,
which keeps the monitor free of credentials. Each fetcher returns a `Release`
for the newest *final* version (no pre-releases, no yanked files, no floating
Docker tags such as `latest` or `1.5`), plus whatever release notes the registry
carries, so the alert can say more than a version number.

The HTTP transport is injectable so tests run on httpx.MockTransport.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .components import Component
from .versions import is_exact_tag, newest, parse

logger = logging.getLogger("lucidlink_api.updates")

PYPI_BASE = "https://pypi.org/pypi"
DOCKERHUB_BASE = "https://hub.docker.com/v2/repositories"
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
NOTES_MAX_CHARS = 12000


class SourceError(Exception):
    """The registry could not be read or answered nonsense."""


@dataclass
class Release:
    version: str
    released_at: Optional[str] = None
    notes: str = ""                       # release notes / description excerpt
    notes_url: Optional[str] = None       # a page a human can open
    all_versions: list[str] = field(default_factory=list)
    digest: Optional[str] = None          # docker: manifest-list digest of the tag
    pinned_digest_now: Optional[str] = None  # docker: digest the pinned tag points at today
    pinned_vulnerabilities: list[dict] = field(default_factory=list)  # pypi: advisories on the pinned version
    platforms: list[str] = field(default_factory=list)  # wheels' platform tags / image architectures


def _get_json(client: httpx.Client, url: str, **kw) -> dict:
    try:
        resp = client.get(url, **kw)
    except httpx.HTTPError as e:
        raise SourceError(f"GET {url}: {type(e).__name__}: {e}") from e
    if resp.status_code != 200:
        raise SourceError(f"GET {url}: HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as e:
        raise SourceError(f"GET {url}: not JSON") from e
    if not isinstance(data, dict):
        raise SourceError(f"GET {url}: unexpected JSON shape")
    return data


def _pypi_live_versions(releases: dict) -> list[str]:
    """Versions with at least one non-yanked file."""
    out = []
    for ver, files in (releases or {}).items():
        if isinstance(files, list) and files and any(not f.get("yanked") for f in files):
            out.append(ver)
    return out


def _wheel_platform(filename: str) -> Optional[str]:
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    return parts[-1] if len(parts) >= 5 else None


def _release_notes_url(info: dict) -> Optional[str]:
    urls = info.get("project_urls") or {}
    for key in urls:
        if re.search(r"release|change|history|news", key, re.IGNORECASE):
            return urls[key]
    return info.get("package_url") or info.get("project_url")


_GITHUB_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?(?:/blob/([^/]+)/([^#?]+))?/?$")
RAW_GITHUB = "https://raw.githubusercontent.com"


def extract_version_section(text: str, version: str) -> str:
    """The markdown section whose heading names `version` (up to the next heading
    of the same or a higher level), or ""."""
    lines = text.splitlines()
    head = re.compile(rf"^(#+)\s*\[?v?{re.escape(version)}(?![\d.])")
    for i, line in enumerate(lines):
        m = head.match(line)
        if not m:
            continue
        level = len(m.group(1))
        out = [line]
        for nxt in lines[i + 1:]:
            h = re.match(r"^(#+)\s", nxt)
            if h and len(h.group(1)) <= level:
                break
            out.append(nxt)
        return "\n".join(out).strip()
    return ""


def fetch_github_notes(client: httpx.Client, url: str, version: str) -> str:
    """Release notes for `version` from a GitHub-hosted markdown file.

    `url` is a project URL such as https://github.com/LucidLink/lucidlink-mcp
    (README.md, then CHANGELOG.md are tried) or a /blob/<ref>/<path> link.
    Best effort: any failure yields "".
    """
    m = _GITHUB_RE.match(url or "")
    if not m:
        return ""
    owner, repo, ref, path = m.groups()
    candidates = [path] if path else ["CHANGELOG.md", "README.md", "RELEASE_NOTES.md"]
    for cand in candidates:
        raw = f"{RAW_GITHUB}/{owner}/{repo}/{ref or 'HEAD'}/{cand}"
        try:
            resp = client.get(raw)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        section = extract_version_section(resp.text, version)
        if section:
            return section
    return ""


def fetch_pypi(client: httpx.Client, component: Component, pinned: Optional[str]) -> Release:
    data = _get_json(client, f"{PYPI_BASE}/{component.package}/json")
    versions = _pypi_live_versions(data.get("releases") or {})
    latest = newest(versions)
    if latest is None:
        raise SourceError(f"{component.package}: no final release on PyPI")

    info = data.get("info") or {}
    files = (data.get("releases") or {}).get(latest) or []
    released_at = min((f.get("upload_time_iso_8601") or f.get("upload_time") or "" for f in files),
                      default="") or None
    platforms = sorted({p for f in files if (p := _wheel_platform(f.get("filename", "")))})

    notes = ""
    notes_url = _release_notes_url(info)
    if info.get("version") == latest:
        notes = info.get("description") or info.get("summary") or ""
    else:
        try:
            vinfo = _get_json(client, f"{PYPI_BASE}/{component.package}/{latest}/json").get("info") or {}
            notes = vinfo.get("description") or vinfo.get("summary") or ""
        except SourceError as e:
            logger.info("no per-version metadata for %s %s: %s", component.package, latest, e)

    if notes_url and "github.com" in notes_url:
        section = fetch_github_notes(client, notes_url, latest)
        if section:
            notes = f"Release notes ({notes_url}):\n{section}\n\nPackage description:\n{notes}"

    vulns: list[dict] = []
    if pinned and parse(pinned) is not None:
        try:
            pdata = _get_json(client, f"{PYPI_BASE}/{component.package}/{pinned}/json")
            for v in pdata.get("vulnerabilities") or []:
                if v.get("withdrawn"):
                    continue
                vulns.append({"id": v.get("id"), "aliases": v.get("aliases") or [],
                              "summary": (v.get("summary") or v.get("details") or "")[:300],
                              "fixed_in": v.get("fixed_in") or [], "link": v.get("link")})
        except SourceError as e:
            logger.info("could not read advisories for %s %s: %s", component.package, pinned, e)

    return Release(version=latest, released_at=released_at, notes=notes[:NOTES_MAX_CHARS],
                   notes_url=notes_url, all_versions=versions, pinned_vulnerabilities=vulns,
                   platforms=platforms)


def _whats_new(full_description: str) -> str:
    """The "What's new ..." sections of a Docker Hub overview, else its tail."""
    m = re.search(r"^#+\s*What'?s new.*", full_description, re.IGNORECASE | re.MULTILINE)
    return full_description[m.start():] if m else full_description[-2000:]


def fetch_dockerhub(client: httpx.Client, component: Component, pinned: Optional[str]) -> Release:
    base = f"{DOCKERHUB_BASE}/{component.package}"
    tags: list[dict] = []
    url: Optional[str] = f"{base}/tags?page_size=100"
    for _ in range(5):  # newest first; 500 tags is plenty to find the top semver
        page = _get_json(client, url)
        tags += [t for t in page.get("results") or [] if isinstance(t, dict)]
        url = page.get("next")
        if not url:
            break

    by_name = {t.get("name"): t for t in tags if t.get("name")}
    exact = [n for n in by_name if is_exact_tag(n)]
    latest = newest(exact)
    if latest is None:
        raise SourceError(f"{component.package}: no X.Y.Z tag on Docker Hub")

    tag = by_name[latest]
    arches = sorted({i.get("architecture") for i in tag.get("images") or []
                     if i.get("architecture") and i.get("architecture") != "unknown"})
    pinned_digest_now = (by_name.get(pinned) or {}).get("digest") if pinned else None

    notes, notes_url = "", f"https://hub.docker.com/r/{component.package}"
    try:
        repo = _get_json(client, f"{base}/")
        notes = _whats_new(repo.get("full_description") or repo.get("description") or "")
    except SourceError as e:
        logger.info("no repository description for %s: %s", component.package, e)

    return Release(version=latest, released_at=tag.get("last_updated") or tag.get("tag_last_pushed"),
                   notes=notes[:NOTES_MAX_CHARS], notes_url=notes_url, all_versions=exact,
                   digest=tag.get("digest"), pinned_digest_now=pinned_digest_now, platforms=arches)


FETCHERS = {"pypi": fetch_pypi, "dockerhub": fetch_dockerhub}


def fetch_latest(client: httpx.Client, component: Component, pinned: Optional[str]) -> Release:
    fetcher = FETCHERS.get(component.source)
    if fetcher is None:
        raise SourceError(f"unknown source {component.source!r}")
    return fetcher(client, component, pinned)
