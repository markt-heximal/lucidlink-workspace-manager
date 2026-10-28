"""Version parsing and patch/minor/major classification, including 0.x."""
import pytest

from updates.versions import classify, count_between, is_exact_tag, newest, parse


@pytest.mark.parametrize("text,expected", [
    ("1.2.3", (1, 2, 3)),
    ("v1.2.3", (1, 2, 3)),
    ("0.14", (0, 14, 0)),
    ("1.0.0rc1", (1, 0, 0)),
    ("2.0.0.post1", (2, 0, 0)),
    ("1.2.3+local.1", (1, 2, 3)),
])
def test_parse_accepts_release_like_versions(text, expected):
    v = parse(text)
    assert v is not None and (v.major, v.minor, v.patch) == expected


@pytest.mark.parametrize("text", ["latest", "1", "3.12-slim", "", None, "main"])
def test_parse_rejects_non_versions(text):
    assert parse(text) is None


def test_ordering_puts_prereleases_before_finals_and_numbers_numerically():
    assert parse("1.0.0rc1") < parse("1.0.0") < parse("1.0.0.post1") < parse("1.0.1")
    assert parse("0.9.0") < parse("0.10.0")  # not string order
    assert parse("1.0.0a1") < parse("1.0.0b1") < parse("1.0.0rc1")


@pytest.mark.parametrize("current,latest,update_type,breaking,auto", [
    ("1.0.0", "1.0.1", "patch", False, True),
    ("1.5.0", "1.5.7", "patch", False, True),
    ("1.5.0", "1.6.0", "minor", False, True),     # minor from >= 1.0: non-breaking
    ("0.14.0", "0.15.0", "minor", True, False),   # 0.x minor: may break
    ("0.14.0", "0.14.1", "patch", False, True),   # 0.x patch: fine
    ("0.0.3", "0.0.4", "patch", True, False),     # 0.0.x: anything may break
    ("0.14.0", "1.0.1", "major", True, False),
    ("1.9.9", "2.0.0", "major", True, False),
    ("1.0.1", "1.0.1", "none", False, False),
    ("1.0.1", "1.0.0", "downgrade", False, False),
    (None, "1.0.0", "unknown", True, False),
    ("1.0.0", "latest", "unknown", True, False),
])
def test_classify(current, latest, update_type, breaking, auto):
    c = classify(current, latest)
    assert (c.update_type, c.breaking, c.automerge_eligible) == (update_type, breaking, auto)


def test_newest_skips_prereleases_and_junk_unless_asked():
    versions = ["0.14.0", "1.0.0", "1.1.0rc1", "latest", "1.0.1"]
    assert newest(versions) == "1.0.1"
    assert newest(versions, include_prereleases=True) == "1.1.0rc1"
    assert newest(["latest"]) is None


def test_count_between_counts_final_releases_after_the_pin():
    versions = ["0.13.0", "0.14.0", "0.15.0", "1.0.0", "1.0.1", "1.1.0rc1"]
    assert count_between(versions, "0.14.0", "1.0.1") == 3
    assert count_between(versions, "1.0.1", "1.0.1") == 0
    assert count_between(versions, None, "1.0.1") == 0


@pytest.mark.parametrize("tag,ok", [("1.5.7", True), ("v1.5.7", True), ("1.5", False),
                                    ("1", False), ("latest", False), ("1.5.7-alpine", False)])
def test_is_exact_tag(tag, ok):
    assert is_exact_tag(tag) is ok
