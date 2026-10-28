"""Version parsing and update classification.

Deliberately not `packaging`: the engine keeps its dependency list short, and
the versions we track are plain `X.Y.Z` (PyPI, PEP 440 finals) or Docker tags
that look the same. Anything else (`latest`, `1.5`, `3.12-slim`) is not a
release and is ignored by `parse()` returning None.

Classification follows semver with the 0.x rule spelled out, because it is the
one that bit us: below 1.0 a *minor* bump is allowed to break the API (the SDK
went 0.14 -> 0.15 -> 1.0 with renames at each step), and below 0.1 even a patch
bump may. `breaking` captures that, separately from the update type, so the
alert and the Renovate policy can agree on what may merge by itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Optional

_VERSION_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?(?:\.(?P<extra>\d+))?"
    r"(?:[-.]?(?P<pre>a|b|c|rc|alpha|beta|pre|preview|dev)[-.]?(?P<pre_n>\d+)?)?"
    r"(?:\.post(?P<post>\d+))?"
    r"(?:\+(?P<local>[0-9A-Za-z.]+))?$",
    re.IGNORECASE,
)

# Order of pre-release labels; finals sort after all of them.
_PRE_ORDER = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 3, "preview": 3, "c": 3, "rc": 3}


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int = 0
    extra: int = 0
    pre: Optional[str] = None
    pre_n: int = 0
    post: int = 0
    raw: str = ""

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None

    def _key(self) -> tuple:
        pre_rank = (_PRE_ORDER.get(self.pre.lower(), 0), self.pre_n) if self.pre else (99, 0)
        return (self.major, self.minor, self.patch, self.extra, pre_rank, self.post)

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        return self.raw or f"{self.major}.{self.minor}.{self.patch}"


def parse(text: Optional[str]) -> Optional[Version]:
    """Parse `1.2.3`, `v1.2`, `1.0.0rc1`, `2.0.0.post1`; None if not a release-like version.

    Two-part versions are accepted (`1.5` -> 1.5.0) so a caller can parse a pin
    like `fastapi==0.115`; Docker floating tags are excluded by `is_exact_tag`.
    """
    if not text:
        return None
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    g = m.groupdict()
    return Version(
        major=int(g["major"]),
        minor=int(g["minor"]),
        patch=int(g["patch"] or 0),
        extra=int(g["extra"] or 0),
        pre=g["pre"].lower() if g["pre"] else None,
        pre_n=int(g["pre_n"] or 0),
        post=int(g["post"] or 0),
        raw=text.strip(),
    )


def is_exact_tag(tag: str) -> bool:
    """True for a full `X.Y.Z` tag; False for floating tags such as `1`, `1.5` or `latest`."""
    return bool(re.fullmatch(r"v?\d+\.\d+\.\d+", tag))


@dataclass(frozen=True)
class Classification:
    """How `latest` relates to `current`.

    update_type: none | patch | minor | major | downgrade | unknown
    breaking: the bump may change the public API (major; minor below 1.0;
        anything below 0.1). Unknown is treated as breaking.
    automerge_eligible: mirrors renovate.json: patch (not from 0.0.x), or
        minor from >= 1.0.
    """
    update_type: str
    breaking: bool
    automerge_eligible: bool


def classify(current: Optional[str], latest: Optional[str]) -> Classification:
    cur, new = parse(current), parse(latest)
    if cur is None or new is None:
        return Classification("unknown", breaking=True, automerge_eligible=False)
    if new == cur:
        return Classification("none", breaking=False, automerge_eligible=False)
    if new < cur:
        return Classification("downgrade", breaking=False, automerge_eligible=False)
    if new.major != cur.major:
        return Classification("major", breaking=True, automerge_eligible=False)
    if new.minor != cur.minor:
        # 0.x: a minor bump is the "major" of pre-1.0 software.
        breaking = cur.major == 0
        return Classification("minor", breaking=breaking, automerge_eligible=not breaking)
    # Same major.minor: patch (or a 4th component / pre / post difference).
    breaking = cur.major == 0 and cur.minor == 0
    return Classification("patch", breaking=breaking, automerge_eligible=not breaking)


def newest(versions: list[str], include_prereleases: bool = False) -> Optional[str]:
    """The highest parseable version string, or None."""
    best: Optional[Version] = None
    for v in versions:
        pv = parse(v)
        if pv is None or (pv.is_prerelease and not include_prereleases):
            continue
        if best is None or pv > best:
            best = pv
    return best.raw if best else None


def count_between(versions: list[str], current: Optional[str], latest: Optional[str]) -> int:
    """How many final releases are > current and <= latest (how far behind the pin is)."""
    cur, new = parse(current), parse(latest)
    if cur is None or new is None:
        return 0
    n = 0
    for v in versions:
        pv = parse(v)
        if pv is not None and not pv.is_prerelease and cur < pv <= new:
            n += 1
    return n
