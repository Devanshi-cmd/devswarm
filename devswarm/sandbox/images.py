"""Build the sandbox image for a repository.

This is the only phase with network access. It installs declared dependencies by name
and never copies the repository in, so nothing from the repo lands in the image.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from devswarm.sandbox.base import SandboxError

_BUILD_TIMEOUT_SECONDS = 900
_REQUIREMENTS_NAME = "requirements.devswarm.txt"

# Accepts "name", "name[extra]" and version or marker suffixes. This rejects "-r ../x",
# "--index-url ...", "git+https://..." and local paths, which could fetch or run
# something other than a named package from the index.
_PLAIN_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(\[[^\]]*\])?\s*([<>=!~;].*)?$")


class LanguageProfile(Protocol):
    """Adding Node later means one more class like PythonProfile, nothing else."""

    @property
    def name(self) -> str: ...

    def matches(self, repo: Path) -> bool: ...
    def dependencies(self, repo: Path) -> list[str]: ...
    def dockerfile(self) -> str: ...
    def test_command(self) -> list[str]: ...


def _plain_requirements(lines: list[str]) -> list[str]:
    cleaned = (line.split(" #")[0].strip() for line in lines)
    return [line for line in cleaned if _PLAIN_REQUIREMENT.match(line)]


def _strings(value: Any) -> list[str]:
    # dependency-groups may hold {include-group = "..."} tables, which we skip.
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


@dataclass(frozen=True)
class PythonProfile:
    name: str = "python"

    def matches(self, repo: Path) -> bool:
        markers = ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg")
        return any((repo / marker).is_file() for marker in markers) or any(repo.glob("*.py"))

    def dependencies(self, repo: Path) -> list[str]:
        found = ["pytest"]
        for name in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
            path = repo / name
            if path.is_file():
                found += _plain_requirements(path.read_text(errors="replace").splitlines())
        pyproject = repo / "pyproject.toml"
        if pyproject.is_file():
            found += _plain_requirements(self._pyproject_dependencies(pyproject))
        return sorted(set(found))

    @staticmethod
    def _pyproject_dependencies(path: Path) -> list[str]:
        try:
            data = tomllib.loads(path.read_text(errors="replace"))
        except tomllib.TOMLDecodeError:
            return []
        project = data.get("project", {})
        found = _strings(project.get("dependencies"))
        optional = project.get("optional-dependencies", {})
        groups = data.get("dependency-groups", {})
        for table in (optional, groups):
            for group in ("dev", "test", "tests"):
                found += _strings(table.get(group))
        return found

    def test_command(self) -> list[str]:
        return ["pytest", "-q"]

    def dockerfile(self) -> str:
        # Tools first: this layer stays cached across every repository and dependency change.
        return (
            "FROM python:3.12-slim\n"
            "RUN apt-get update"
            " && apt-get install -y --no-install-recommends git ripgrep"
            " && rm -rf /var/lib/apt/lists/*\n"
            f"COPY {_REQUIREMENTS_NAME} /opt/devswarm/requirements.txt\n"
            "RUN pip install --no-cache-dir --disable-pip-version-check"
            " -r /opt/devswarm/requirements.txt\n"
        )


PROFILES: tuple[LanguageProfile, ...] = (PythonProfile(),)


def detect_profile(repo: Path) -> LanguageProfile:
    for profile in PROFILES:
        if profile.matches(repo):
            return profile
    raise SandboxError(f"no language profile matches this repository: {repo.name}")


def image_tag(profile: LanguageProfile, dockerfile: str, dependencies: list[str]) -> str:
    digest = hashlib.sha256("\n".join([dockerfile, *dependencies]).encode()).hexdigest()[:16]
    return f"devswarm-sandbox-{profile.name}:{digest}"


def _docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise SandboxError("the docker command is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"docker {args[0]} timed out after {timeout}s") from exc


def ensure_image(repo: Path) -> str:
    """Return a tag for this repo's image, building it only if it does not exist yet."""
    profile = detect_profile(repo)
    dockerfile = profile.dockerfile()
    dependencies = profile.dependencies(repo)
    tag = image_tag(profile, dockerfile, dependencies)
    if _docker(["image", "inspect", tag], timeout=30).returncode == 0:
        return tag

    with tempfile.TemporaryDirectory(prefix="devswarm-build-") as context:
        (Path(context) / "Dockerfile").write_text(dockerfile)
        (Path(context) / _REQUIREMENTS_NAME).write_text("\n".join(dependencies) + "\n")
        result = _docker(["build", "--quiet", "--tag", tag, context], _BUILD_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise SandboxError(f"image build failed:\n{result.stderr[-4000:]}")
    return tag
