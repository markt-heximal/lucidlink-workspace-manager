"""Reading pins from requirements.txt, docker-compose.yml and .mcp.json.

The real-file tests read the expected value out of the file themselves, so a
Renovate bump never has to touch this test.
"""
import json
import re
from importlib import metadata
from pathlib import Path

from updates.components import (Component, load_components, parse_image_pin,
                                parse_requirements_pin, parse_uvx_pin, resolve_pin)
from upd_helpers import docker_component, pypi_component

DOCKER_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = DOCKER_DIR.parent


def test_parse_requirements_pin():
    text = "# c\nfastapi==0.115.12\nlucidlink==0.14.0  # sdk\nlucidlink-mcp==0.3.1\nhttpx[http2]==0.28.1\n"
    assert parse_requirements_pin(text, "lucidlink") == "0.14.0"
    assert parse_requirements_pin(text, "lucidlink-mcp") == "0.3.1"
    assert parse_requirements_pin(text, "httpx") == "0.28.1"
    assert parse_requirements_pin(text, "numpy") is None


def test_parse_image_pin_with_and_without_digest():
    digest = "sha256:" + "f" * 64
    assert parse_image_pin(f"    image: acme/api:1.5.0@{digest}\n", "acme/api") == ("1.5.0", digest)
    assert parse_image_pin("image: \"acme/api:2.0.0\"\n", "acme/api") == ("2.0.0", None)
    assert parse_image_pin("image: other/api:1.0.0\n", "acme/api") == (None, None)


def test_parse_uvx_pin():
    text = json.dumps({"mcpServers": {"x": {"command": "uvx", "args": ["lucidlink-mcp==0.3.1"]}}})
    assert parse_uvx_pin(text, "lucidlink-mcp") == "0.3.1"
    assert parse_uvx_pin(text, "other") is None


def test_default_components_resolve_against_the_real_repo_files():
    comps = {c.key: c for c in load_components()}
    assert set(comps) >= {"lucidlink", "lucidlink-api", "lucidlink-mcp"}
    dirs = [DOCKER_DIR, REPO_ROOT]

    req = (DOCKER_DIR / "requirements.txt").read_text()
    sdk = resolve_pin(comps["lucidlink"], dirs)
    assert sdk.declared == re.search(r"^lucidlink==(\S+)", req, re.M).group(1)

    compose = (DOCKER_DIR / "docker-compose.yml").read_text()
    m = re.search(r"lucidlink/lucidlink-api:([^@\s]+)@(sha256:[0-9a-f]{64})", compose)
    api = resolve_pin(comps["lucidlink-api"], dirs)
    assert (api.version, api.digest) == (m.group(1), m.group(2))

    mcp_cfg = (REPO_ROOT / ".mcp.json").read_text()
    mcp = resolve_pin(comps["lucidlink-mcp"], dirs)
    assert mcp.version == re.search(r"lucidlink-mcp==([^\"]+)", mcp_cfg).group(1)


def test_installed_version_wins_and_drift_is_visible(tmp_path):
    installed = metadata.version("httpx")
    (tmp_path / "requirements.txt").write_text("httpx==99.0.0\n")
    comp = Component(key="h", label="httpx", source="pypi", package="httpx",
                     pin={"file": "requirements.txt", "kind": "requirements", "name": "httpx",
                          "installed": "httpx"})
    pin = resolve_pin(comp, [tmp_path])
    assert (pin.version, pin.source, pin.declared) == (installed, "installed", "99.0.0")


def test_env_override_beats_everything(monkeypatch, pin_dir):
    monkeypatch.setenv("UPDATES_PIN_SDK", "9.9.9")
    pin = resolve_pin(pypi_component(), [pin_dir])
    assert pin.version == "9.9.9" and pin.declared == "0.14.0"
    assert pin.source == "env UPDATES_PIN_SDK"


def test_missing_pin_file_says_how_to_fix(tmp_path):
    pin = resolve_pin(docker_component(), [tmp_path])
    assert pin.version is None and "UPDATES_PIN_IMG" in pin.source


def test_components_file_adds_and_overrides(monkeypatch, tmp_path):
    f = tmp_path / "components.json"
    f.write_text(json.dumps([
        {"key": "extra", "source": "pypi", "package": "extra-pkg"},
        {"key": "lucidlink-mcp", "source": "pypi", "package": "lucidlink-mcp", "label": "MCP (custom)"},
        {"key": "bogus", "source": "npm", "package": "x"},
    ]))
    monkeypatch.setenv("UPDATES_COMPONENTS_FILE", str(f))
    comps = {c.key: c for c in load_components()}
    assert "extra" in comps and "bogus" not in comps
    assert comps["lucidlink-mcp"].label == "MCP (custom)"
