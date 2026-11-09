"""Language support: how to detect, build and test each kind of repository.

Building the image is the only phase with network access. Only dependency manifests go
into it, never source code. Each language has one profile class here, and adding a
language means adding one more.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import tomllib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from devswarm.sandbox.base import SandboxError, SandboxLimits

_BUILD_TIMEOUT_SECONDS = 3_600
_REQUIREMENTS_NAME = "requirements.devswarm.txt"
_PROJECT_DIRECTORY = "project"
_MAX_MANIFEST_DEPTH = 2
# Not worth scanning: vendored code and build output say nothing about the project.
_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        "target",
        "vendor",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".devswarm-target",
    }
)
_RUST_TARGET_DIRECTORY = ".devswarm-target"
_INSTALL_TOOLS = (
    "RUN apt-get update && apt-get install -y --no-install-recommends "
    "git ripgrep ca-certificates{extra} && rm -rf /var/lib/apt/lists/*\n"
)

# Accepts "name", "name[extra]" and version or marker suffixes. This rejects "-r ../x",
# "--index-url ...", "git+https://..." and local paths, which could fetch or run
# something other than a named package from the index.
_PLAIN_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(\[[^\]]*\])?\s*([<>=!~;].*)?$")

_PYTEST_FAILURE = re.compile(
    r"^(?:FAILED|ERROR) (?P<test>.+?)(?: - (?P<message>.*))?$", re.MULTILINE
)
_PYTHON_ERROR_TYPE = re.compile(r"^([\w.]*(?:Error|Exception|Warning|Exit|Interrupt))\b")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_JS_FAILURE = re.compile(r"^\s*(?:FAIL|×|✕|●)\s+(?P<test>.+?)\s*$", re.MULTILINE)
_NODE_TEST_FAILURE = re.compile(r"^\s*not ok \d+ - (?P<test>.+?)\s*$", re.MULTILINE)
_JS_DURATION = re.compile(r"\s+\d+(?:\.\d+)?\s*m?s$")
_RUST_TEST_FAILURE = re.compile(r"^test (?P<test>\S+) \.\.\. FAILED$", re.MULTILINE)
_RUST_FAILURE_SUMMARY = re.compile(r"^failures:\n((?:    \S+\n)+)", re.MULTILINE)
_RUST_COMPILE_ERROR = re.compile(r"^error\[(?P<code>E\d+)\]", re.MULTILINE)


class LanguageProfile(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def display_name(self) -> str: ...
    @property
    def source_suffixes(self) -> tuple[str, ...]: ...
    @property
    def github_languages(self) -> tuple[str, ...]: ...
    @property
    def limits(self) -> SandboxLimits: ...
    @property
    def test_timeout(self) -> int: ...
    @property
    def max_seconds(self) -> float: ...
    @property
    def generated_paths(self) -> tuple[str, ...]: ...

    def matches(self, repo: Path) -> bool: ...
    def for_repo(self, repo: Path) -> LanguageProfile: ...
    def build_context(self, repo: Path) -> dict[str, bytes]: ...
    def dockerfile(self) -> str: ...
    def setup_command(self) -> list[str] | None: ...
    def test_command(self) -> list[str]: ...
    def is_test_path(self, path: str) -> bool: ...
    def failure_fingerprints(self, output: str) -> list[str]: ...
    def project_notes(self) -> str: ...


def _walk_files(repo: Path) -> Iterator[Path]:
    """Every regular file worth looking at. Symlinks are skipped: a repo could point one
    at a file on the host, and these contents go into the image build."""
    for root, directories, names in os.walk(repo):
        directories[:] = [name for name in directories if name not in _SKIPPED_DIRECTORIES]
        for name in names:
            path = Path(root) / name
            if not path.is_symlink() and path.is_file():
                yield path


def _read_regular(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() and not path.is_symlink() else None


def _plain_requirements(lines: list[str]) -> list[str]:
    cleaned = (line.split(" #")[0].strip() for line in lines)
    return [line for line in cleaned if _PLAIN_REQUIREMENT.match(line)]


def _strings(value: Any) -> list[str]:
    # dependency-groups may hold {include-group = "..."} tables, which we skip.
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


@dataclass(frozen=True)
class PythonProfile:
    name: str = "python"
    display_name: str = "Python"
    source_suffixes: tuple[str, ...] = (".py",)
    github_languages: tuple[str, ...] = ("Python", "Jupyter Notebook")
    limits: SandboxLimits = SandboxLimits()
    test_timeout: int = 300
    max_seconds: float = 1_200
    generated_paths: tuple[str, ...] = ()

    def matches(self, repo: Path) -> bool:
        markers = ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg")
        return any((repo / marker).is_file() for marker in markers) or any(repo.glob("*.py"))

    def for_repo(self, repo: Path) -> PythonProfile:
        return self

    def dependencies(self, repo: Path) -> list[str]:
        found = ["pytest"]
        for name in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
            data = _read_regular(repo / name)
            if data is not None:
                found += _plain_requirements(data.decode(errors="replace").splitlines())
        pyproject = repo / "pyproject.toml"
        if _read_regular(pyproject) is not None:
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

    def build_context(self, repo: Path) -> dict[str, bytes]:
        return {_REQUIREMENTS_NAME: ("\n".join(self.dependencies(repo)) + "\n").encode()}

    def setup_command(self) -> list[str] | None:
        return None

    def test_command(self) -> list[str]:
        return ["pytest", "-q"]

    def is_test_path(self, path: str) -> bool:
        pure = PurePosixPath(path)
        name = pure.name
        return (
            any(part in ("test", "tests") for part in pure.parts[:-1])
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
        )

    def failure_fingerprints(self, output: str) -> list[str]:
        """Identify each failure by test and error type from pytest's summary.

        Full messages differ between runs (addresses, timings), which would hide a repeat.
        """
        fingerprints = []
        for match in _PYTEST_FAILURE.finditer(output):
            kind = _PYTHON_ERROR_TYPE.match(match["message"] or "")
            fingerprints.append(f"{match['test']}|{kind[1] if kind else 'assertion'}")
        return sorted(set(fingerprints))

    def project_notes(self) -> str:
        return "language: Python\ntest_command: pytest -q"

    def dockerfile(self) -> str:
        # Tools first: this layer stays cached across every repository and dependency change.
        return (
            "FROM python:3.12-slim\n"
            + _INSTALL_TOOLS.format(extra="")
            + f"COPY {_REQUIREMENTS_NAME} /opt/devswarm/requirements.txt\n"
            "RUN pip install --no-cache-dir --disable-pip-version-check"
            " -r /opt/devswarm/requirements.txt\n"
        )


_NODE_LOCKFILES = ("pnpm-lock.yaml", "yarn.lock", "package-lock.json")
_NODE_MANAGERS = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "package-lock.json": "npm"}
# --ignore-scripts because install scripts are arbitrary code, and this step has network.
_NODE_INSTALL = {
    "npm": "npm ci --ignore-scripts || npm install --ignore-scripts",
    "pnpm": (
        "pnpm install --frozen-lockfile --ignore-scripts"
        " || pnpm install --no-frozen-lockfile --ignore-scripts"
    ),
    "yarn": "yarn install --ignore-scripts",
}
_NODE_ROOT_FILES = ("pnpm-workspace.yaml", ".yarnrc.yml", *_NODE_LOCKFILES)


@dataclass(frozen=True)
class NodeProfile:
    name: str = "node"
    display_name: str = "JavaScript/TypeScript"
    source_suffixes: tuple[str, ...] = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
    github_languages: tuple[str, ...] = ("JavaScript", "TypeScript")
    limits: SandboxLimits = SandboxLimits(cpus="3", memory="3g", pids=512)
    test_timeout: int = 600
    max_seconds: float = 1_800
    generated_paths: tuple[str, ...] = ("node_modules",)
    manager: str = "npm"

    def matches(self, repo: Path) -> bool:
        return (repo / "package.json").is_file()

    def for_repo(self, repo: Path) -> NodeProfile:
        lockfile = next((name for name in _NODE_LOCKFILES if (repo / name).is_file()), None)
        return replace(self, manager=_NODE_MANAGERS[lockfile] if lockfile else "npm")

    def build_context(self, repo: Path) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for path in _walk_files(repo):
            relative = path.relative_to(repo)
            at_root = len(relative.parts) == 1
            if (
                relative.name == "package.json"
                or (at_root and relative.name in _NODE_ROOT_FILES)
                # pnpm applies these while installing, and fails if they are missing.
                or relative.parts[0] == "patches"
            ):
                files[f"{_PROJECT_DIRECTORY}/{relative.as_posix()}"] = path.read_bytes()
        return files

    def dockerfile(self) -> str:
        return (
            "FROM node:22-slim\n"
            + _INSTALL_TOOLS.format(extra="")
            + "ENV COREPACK_HOME=/opt/corepack COREPACK_ENABLE_DOWNLOAD_PROMPT=0 CI=true\n"
            "RUN corepack enable\n"
            "WORKDIR /deps\n"
            f"COPY {_PROJECT_DIRECTORY}/ /deps/\n"
            f"RUN {_NODE_INSTALL[self.manager]}\n"
            "RUN mkdir -p /opt/corepack && chmod -R a+rX /opt/corepack\n"
            "ENV COREPACK_ENABLE_NETWORK=0\n"
        )

    def setup_command(self) -> list[str]:
        # Copied in rather than mounted, because each package expects its own node_modules.
        return [
            "bash",
            "-c",
            "cd /deps && find . -type d -name node_modules -prune -print0"
            " | xargs -0 -r cp -a --parents -t /workspace",
        ]

    def test_command(self) -> list[str]:
        return [self.manager, "test"]

    def is_test_path(self, path: str) -> bool:
        pure = PurePosixPath(path)
        folders = ("test", "tests", "__tests__", "__mocks__", "e2e")
        return (
            any(part in folders for part in pure.parts[:-1])
            or ".test." in pure.name
            or ".spec." in pure.name
        )

    def failure_fingerprints(self, output: str) -> list[str]:
        text = _ANSI_ESCAPE.sub("", output)
        names = [match["test"] for match in _JS_FAILURE.finditer(text)]
        names += [match["test"] for match in _NODE_TEST_FAILURE.finditer(text)]
        return sorted({f"{_JS_DURATION.sub('', name)}|assertion" for name in names})

    def project_notes(self) -> str:
        return (
            "language: JavaScript/TypeScript\n"
            f"test_command: {self.manager} test\n"
            "note: dependencies are already installed and there is no network"
        )


def _manifest_depth(path: Path, repo: Path) -> int:
    return len(path.relative_to(repo).parts) - 1


def _find_cargo_manifests(repo: Path) -> list[Path]:
    return sorted(
        (path for path in _walk_files(repo) if path.name == "Cargo.toml"),
        key=lambda path: (_manifest_depth(path, repo), str(path)),
    )


def _safe_relative(value: object) -> str | None:
    """A manifest's path entry, unless it would point outside the build context."""
    if not isinstance(value, str) or not value:
        return None
    pure = PurePosixPath(value)
    return None if pure.is_absolute() or ".." in pure.parts else value


def _cargo_stub_paths(manifest: Path) -> set[str]:
    """Source files cargo insists on seeing, relative to this manifest.

    Cargo won't read a package with no targets. Empty stand-ins satisfy it, so the
    project's real code, and its build script, stay out of the phase with network.
    """
    try:
        data = tomllib.loads(manifest.read_text(errors="replace"))
    except tomllib.TOMLDecodeError:
        return set()
    if "package" not in data:
        return set()

    directory = manifest.parent
    found: set[str] = set()
    for default in ("src/lib.rs", "src/main.rs"):
        if (directory / default).is_file():
            found.add(default)
    if (directory / "src" / "bin").is_dir():
        found |= {f"src/bin/{path.name}" for path in (directory / "src" / "bin").glob("*.rs")}

    declared = [data.get("lib", {})]
    for section in ("bin", "test", "bench", "example"):
        items = data.get(section, [])
        declared += items if isinstance(items, list) else []
    for item in declared:
        path = _safe_relative(item.get("path")) if isinstance(item, dict) else None
        if path:
            found.add(path)
    build_script = _safe_relative(data["package"].get("build"))
    if build_script:
        found.add(build_script)
    return found or {"src/lib.rs"}


_CARGO_BUILD_JOBS = 3


@dataclass(frozen=True)
class RustProfile:
    name: str = "rust"
    display_name: str = "Rust"
    source_suffixes: tuple[str, ...] = (".rs",)
    github_languages: tuple[str, ...] = ("Rust",)
    # Compilers need room, but this machine is shared.
    limits: SandboxLimits = SandboxLimits(cpus="4", memory="4g", pids=1_024, tmpfs_size="1g")
    test_timeout: int = 1_200
    max_seconds: float = 2_700
    generated_paths: tuple[str, ...] = (_RUST_TARGET_DIRECTORY,)
    manifest: str = "Cargo.toml"

    def matches(self, repo: Path) -> bool:
        return any(
            _manifest_depth(path, repo) <= _MAX_MANIFEST_DEPTH
            for path in _find_cargo_manifests(repo)
        )

    def for_repo(self, repo: Path) -> RustProfile:
        manifests = _find_cargo_manifests(repo)
        if not manifests:
            return self
        return replace(self, manifest=manifests[0].relative_to(repo).as_posix())

    def build_context(self, repo: Path) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for path in _walk_files(repo):
            relative = path.relative_to(repo)
            if relative.name in (
                "Cargo.toml",
                "Cargo.lock",
                "rust-toolchain",
                "rust-toolchain.toml",
            ):
                files[f"{_PROJECT_DIRECTORY}/{relative.as_posix()}"] = path.read_bytes()
            if relative.name == "Cargo.toml":
                folder = relative.parent
                for stub in _cargo_stub_paths(path):
                    name = f"{_PROJECT_DIRECTORY}/{(folder / stub).as_posix()}"
                    files[name] = b"fn main() {}\n"
        return files

    def dockerfile(self) -> str:
        crate_directory = PurePosixPath(self.manifest).parent.as_posix()
        return (
            "FROM rust:1-slim-bookworm\n"
            + _INSTALL_TOOLS.format(extra=" pkg-config libssl-dev build-essential")
            +  # Debug info is the biggest memory cost of compiling, and tests don't need it.
            "ENV CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0 CARGO_INCREMENTAL=0\n"
            f"ENV CARGO_TARGET_DIR=/deps/target CARGO_BUILD_JOBS={_CARGO_BUILD_JOBS}\n"
            f"COPY {_PROJECT_DIRECTORY}/ /deps/\n"
            f"WORKDIR /deps/{crate_directory}\n"
            "RUN cargo fetch\n"
            # Compiles dependencies ahead of time. The project's own stubs may fail, which
            # is fine: everything built before that failure is still cached.
            "RUN cargo build --all-targets --offline --keep-going || true\n"
            # The sandbox runs as an ordinary user, who needs to read all of this.
            "RUN mkdir -p /deps/target && chmod -R a+rX /deps/target /usr/local/cargo\n"
            # One compile at a time from here. Dependencies are done, and the project's own
            # binary and test binary can each need gigabytes, too much to build together.
            f"ENV CARGO_TARGET_DIR=/workspace/{_RUST_TARGET_DIRECTORY} CARGO_NET_OFFLINE=true"
            " CARGO_BUILD_JOBS=1\n"
        )

    def setup_command(self) -> list[str]:
        target = f"/workspace/{_RUST_TARGET_DIRECTORY}"
        # The image's cache was built from empty stubs, and cargo goes by timestamps. Real
        # files can look older than those stubs, so cargo would test the stubs instead.
        return [
            "bash",
            "-c",
            f"if [ -d /deps/target ]; then mkdir -p {target} && cp -a /deps/target/. {target}/; fi"
            f" && find /workspace -path {target} -prune -o -name '*.rs' -type f -exec touch {{}} +",
        ]

    def test_command(self) -> list[str]:
        return ["cargo", "test", "--manifest-path", self.manifest, "--offline", "--color", "never"]

    def is_test_path(self, path: str) -> bool:
        return "tests" in PurePosixPath(path).parts[:-1]

    def failure_fingerprints(self, output: str) -> list[str]:
        found = {f"{match['test']}|assertion" for match in _RUST_TEST_FAILURE.finditer(output)}
        # Long output is cut in the middle, so the end-of-run summary is the reliable list.
        for block in _RUST_FAILURE_SUMMARY.findall(output):
            found |= {f"{line.strip()}|assertion" for line in block.splitlines()}
        found |= {f"compile|{match['code']}" for match in _RUST_COMPILE_ERROR.finditer(output)}
        return sorted(found)

    def project_notes(self) -> str:
        return (
            "language: Rust\n"
            f"test_command: {' '.join(self.test_command())}\n"
            "note: dependencies are already downloaded and there is no network\n"
            "note: the first compile can take minutes, so give cargo a timeout of 600 seconds\n"
            "note: the Tester's new tests must go in a tests/ directory, because its changes "
            "anywhere else are undone. Inline #[cfg(test)] tests belong to the Coder."
        )


PROFILES: tuple[LanguageProfile, ...] = (PythonProfile(), NodeProfile(), RustProfile())
SUPPORTED_GITHUB_LANGUAGES = frozenset(
    language for profile in PROFILES for language in profile.github_languages
)
SUPPORTED_LANGUAGE_NAMES = ", ".join(profile.display_name for profile in PROFILES)


def _source_bytes(repo: Path) -> Counter[str]:
    sizes: Counter[str] = Counter()
    for path in _walk_files(repo):
        sizes[path.suffix] += path.stat().st_size
    return sizes


def detect_profile(repo: Path) -> LanguageProfile:
    """Choose a profile. In a mixed repository, the language with the most code wins."""
    matching = [profile for profile in PROFILES if profile.matches(repo)]
    if not matching:
        raise SandboxError(f"no language profile matches this repository: {repo.name}")
    if len(matching) == 1:
        return matching[0].for_repo(repo)
    sizes = _source_bytes(repo)
    return max(
        matching, key=lambda profile: sum(sizes[suffix] for suffix in profile.source_suffixes)
    ).for_repo(repo)


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
    files = profile.build_context(repo)
    fingerprint = [
        f"{name}:{hashlib.sha256(data).hexdigest()}" for name, data in sorted(files.items())
    ]
    tag = image_tag(profile, dockerfile, fingerprint)
    if _docker(["image", "inspect", tag], timeout=30).returncode == 0:
        return tag

    with tempfile.TemporaryDirectory(prefix="devswarm-build-") as context:
        (Path(context) / "Dockerfile").write_text(dockerfile)
        for name, data in files.items():
            target = Path(context) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        result = _docker(["build", "--quiet", "--tag", tag, context], _BUILD_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise SandboxError(f"image build failed:\n{result.stderr[-4000:]}")
    return tag
