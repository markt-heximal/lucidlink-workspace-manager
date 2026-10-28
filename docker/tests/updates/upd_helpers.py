"""Fakes shared by the update-monitor tests (imported by conftest and tests).

A uniquely named module rather than `from conftest import ...`, so it cannot
collide with another test package's conftest.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

from updates.components import Component


def pypi_component(key: str = "sdk", package: str = "fakesdk", public: bool = True) -> Component:
    return Component(key=key, label=f"Fake {key}", source="pypi", package=package,
                     pin={"file": "requirements.txt", "kind": "requirements", "name": package},
                     relevance="test component", public=public)


def docker_component(key: str = "img", package: str = "acme/api") -> Component:
    return Component(key=key, label=f"Fake {key}", source="dockerhub", package=package,
                     pin={"file": "docker-compose.yml", "kind": "image", "name": package},
                     relevance="test image", public=True)


class FakeRegistry:
    """PyPI + Docker Hub + raw.githubusercontent.com, mutable so a test can
    'publish' a release between checks. Records every request."""

    def __init__(self):
        self.pypi: dict[str, dict] = {}
        self.vulns: dict[tuple[str, str], list[dict]] = {}
        self.docker: dict[str, list[dict]] = {}
        self.docker_desc: dict[str, str] = {}
        self.github: dict[str, str] = {}
        self.fail: set[str] = set()       # package names that answer 503
        self.requests: list[httpx.Request] = []

    # -- publishing -------------------------------------------------------
    def publish_pypi(self, package: str, version: str, *, yanked: bool = False,
                     description: str = "", project_urls: Optional[dict] = None,
                     uploaded: str = "2026-10-24T16:13:11") -> None:
        pkg = self.pypi.setdefault(package, {"releases": {}, "descriptions": {}, "project_urls": {}})
        pkg["releases"][version] = [{
            "filename": f"{package}-{version}-py3-none-manylinux_2_35_x86_64.whl",
            "upload_time": uploaded, "upload_time_iso_8601": uploaded + "Z", "yanked": yanked}]
        pkg["descriptions"][version] = description or f"{package} {version} description"
        if project_urls:
            pkg["project_urls"] = project_urls

    def publish_docker(self, repo: str, tag: str, digest: str = "", arches=("amd64", "arm64")) -> None:
        tags = self.docker.setdefault(repo, [])
        tags.insert(0, {"name": tag, "digest": digest or f"sha256:{(tag * 64)[:64]}",
                        "last_updated": "2026-10-24T15:55:54Z",
                        "images": [{"architecture": a} for a in arches]})

    # -- transport --------------------------------------------------------
    def _pypi(self, path: str) -> httpx.Response:
        parts = path.strip("/").split("/")          # pypi/<pkg>/json | pypi/<pkg>/<ver>/json
        pkg_name = parts[1]
        if pkg_name in self.fail:
            return httpx.Response(503)
        pkg = self.pypi.get(pkg_name)
        if pkg is None:
            return httpx.Response(404)
        live = [v for v, f in pkg["releases"].items() if not all(x["yanked"] for x in f)]
        top = max(live, key=lambda v: [int(p) if p.isdigit() else -1 for p in v.replace("rc", ".").split(".")],
                  default=None)
        if len(parts) == 3:
            ver = top
            body = {"info": {"version": top, "description": pkg["descriptions"].get(top, ""),
                             "project_urls": pkg["project_urls"]},
                    "releases": pkg["releases"], "urls": pkg["releases"].get(top, [])}
        else:
            ver = parts[2]
            if ver not in pkg["releases"]:
                return httpx.Response(404)
            body = {"info": {"version": ver, "description": pkg["descriptions"].get(ver, ""),
                             "project_urls": pkg["project_urls"]},
                    "urls": pkg["releases"][ver],
                    "vulnerabilities": self.vulns.get((pkg_name, ver), [])}
        return httpx.Response(200, json=body)

    def _docker(self, path: str) -> httpx.Response:
        # /v2/repositories/<ns>/<repo>/tags  or  /v2/repositories/<ns>/<repo>/
        rest = path[len("/v2/repositories/"):]
        repo = "/".join(rest.split("/")[:2])
        if repo in self.fail:
            return httpx.Response(503)
        if repo not in self.docker:
            return httpx.Response(404)
        if rest.rstrip("/").endswith("/tags"):
            return httpx.Response(200, json={"count": len(self.docker[repo]), "next": None,
                                             "results": self.docker[repo]})
        return httpx.Response(200, json={"description": "fake",
                                         "full_description": self.docker_desc.get(repo, "")})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "pypi.org":
            return self._pypi(path)
        if host == "hub.docker.com":
            return self._docker(path)
        if host == "raw.githubusercontent.com":
            text = self.github.get(path)
            return httpx.Response(200, text=text) if text is not None else httpx.Response(404)
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class Recorder:
    """A notification endpoint: records JSON bodies; status per host is settable."""

    def __init__(self):
        self.calls: list[tuple[str, dict, dict]] = []   # (url, json, headers)
        self.status: dict[str, int] = {}
        self.raise_for: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self.raise_for:
            raise httpx.ConnectError(f"cannot reach https://{host}/?token=SECRET", request=request)
        self.calls.append((str(request.url), json.loads(request.content or b"{}"), dict(request.headers)))
        return httpx.Response(self.status.get(host, 200), json={"id": "ok"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def hosts(self) -> list[str]:
        return [httpx.URL(u).host for u, _, _ in self.calls]


def sse_llm(answer: str = "- Breaking: renamed fs to file_system.\n- Upgrade after migrating.",
            calls: Optional[list] = None, status: int = 200) -> httpx.MockTransport:
    """An OpenAI-compatible streaming chat endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        if status >= 400:
            return httpx.Response(status, text="boom")
        chunks = [answer[i:i + 15] for i in range(0, len(answer), 15)]
        body = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n" for c in chunks)
        return httpx.Response(200, text=body + "data: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)
