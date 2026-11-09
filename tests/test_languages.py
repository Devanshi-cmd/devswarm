"""Language profiles, offline. Real builds are checked by test_language_sandboxes.py."""

from pathlib import Path

import pytest
from fakes import FakeSandbox, result

from devswarm.github.local import remove_generated
from devswarm.orchestrator.verify import SandboxTestRunner
from devswarm.sandbox.base import CommandResult, SandboxError
from devswarm.sandbox.images import (
    SUPPORTED_GITHUB_LANGUAGES,
    NodeProfile,
    PythonProfile,
    RustProfile,
    detect_profile,
)

CRATE = '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'


def write(root: Path, name: str, content: str = "") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_detects_python(tmp_path: Path) -> None:
    write(tmp_path, "main.py")
    assert detect_profile(tmp_path).name == "python"


def test_detects_node(tmp_path: Path) -> None:
    write(tmp_path, "package.json", "{}")
    assert detect_profile(tmp_path).name == "node"


def test_detects_rust_at_the_root(tmp_path: Path) -> None:
    write(tmp_path, "Cargo.toml", CRATE)
    write(tmp_path, "src/lib.rs")
    profile = detect_profile(tmp_path)
    assert profile.name == "rust"
    assert "Cargo.toml" in profile.test_command()


def test_finds_a_rust_crate_in_a_subfolder(tmp_path: Path) -> None:
    write(tmp_path, "cli/Cargo.toml", CRATE)
    write(tmp_path, "cli/src/main.rs")
    profile = detect_profile(tmp_path)
    assert profile.name == "rust"
    assert "cli/Cargo.toml" in profile.test_command()
    assert "WORKDIR /deps/cli" in profile.dockerfile()


def test_outermost_cargo_manifest_wins(tmp_path: Path) -> None:
    write(tmp_path, "cli/Cargo.toml", CRATE)
    write(tmp_path, "cli/inner/Cargo.toml", CRATE)
    assert "cli/Cargo.toml" in detect_profile(tmp_path).test_command()


def test_mixed_repo_uses_the_language_with_the_most_code(tmp_path: Path) -> None:
    write(tmp_path, "package.json", "{}")
    write(tmp_path, "app.js", "x" * 10)
    write(tmp_path, "cli/Cargo.toml", CRATE)
    write(tmp_path, "cli/src/main.rs", "x" * 5000)
    assert detect_profile(tmp_path).name == "rust"


def test_mixed_repo_can_be_mostly_node(tmp_path: Path) -> None:
    write(tmp_path, "package.json", "{}")
    write(tmp_path, "src/app.ts", "x" * 5000)
    write(tmp_path, "tool/Cargo.toml", CRATE)
    write(tmp_path, "tool/src/main.rs", "x")
    assert detect_profile(tmp_path).name == "node"


def test_vendored_and_build_output_are_not_counted(tmp_path: Path) -> None:
    write(tmp_path, "package.json", "{}")
    write(tmp_path, "src/app.ts", "x" * 100)
    write(tmp_path, "tool/Cargo.toml", CRATE)
    write(tmp_path, "tool/src/main.rs", "x" * 10)
    write(tmp_path, "tool/target/generated.rs", "x" * 9000)
    assert detect_profile(tmp_path).name == "node"


def test_unsupported_repo_is_refused(tmp_path: Path) -> None:
    write(tmp_path, "main.go")
    with pytest.raises(SandboxError, match="no language profile"):
        detect_profile(tmp_path)


@pytest.mark.parametrize(
    ("lockfile", "manager"),
    [("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm")],
)
def test_node_package_manager_follows_the_lockfile(
    tmp_path: Path, lockfile: str, manager: str
) -> None:
    write(tmp_path, "package.json", "{}")
    write(tmp_path, lockfile)
    profile = detect_profile(tmp_path)
    assert profile.test_command() == [manager, "test"]
    assert manager in profile.dockerfile()


def test_node_defaults_to_npm(tmp_path: Path) -> None:
    write(tmp_path, "package.json", "{}")
    assert detect_profile(tmp_path).test_command() == ["npm", "test"]


def test_node_install_never_runs_package_scripts() -> None:
    for manager in ("npm", "pnpm", "yarn"):
        for command in NodeProfile(manager=manager).dockerfile().splitlines():
            if " install" in command and command.startswith("RUN") and manager in command:
                assert "--ignore-scripts" in command


def test_node_build_context_has_manifests_but_no_source(tmp_path: Path) -> None:
    write(tmp_path, "package.json", "{}")
    write(tmp_path, "pnpm-lock.yaml", "x")
    write(tmp_path, "pnpm-workspace.yaml", "x")
    write(tmp_path, "packages/a/package.json", "{}")
    write(tmp_path, "patches/fix.patch", "x")
    write(tmp_path, ".npmrc", "registry=https://evil.example")
    write(tmp_path, "src/secret.ts", "code")
    write(tmp_path, "node_modules/dep/package.json", "{}")
    files = NodeProfile().build_context(tmp_path)
    assert set(files) == {
        "project/package.json",
        "project/pnpm-lock.yaml",
        "project/pnpm-workspace.yaml",
        "project/packages/a/package.json",
        "project/patches/fix.patch",
    }


def test_rust_build_context_has_manifests_and_empty_stubs_only(tmp_path: Path) -> None:
    write(tmp_path, "cli/Cargo.toml", CRATE)
    write(tmp_path, "cli/Cargo.lock", "lock")
    write(tmp_path, "cli/build.rs", "panic!()")
    write(tmp_path, "cli/src/main.rs", "SECRET SOURCE")
    write(tmp_path, "cli/src/util.rs", "SECRET SOURCE")
    files = RustProfile().build_context(tmp_path)
    assert set(files) == {
        "project/cli/Cargo.toml",
        "project/cli/Cargo.lock",
        "project/cli/src/main.rs",
    }
    assert files["project/cli/src/main.rs"] == b"fn main() {}\n"


def test_rust_stubs_cover_declared_targets(tmp_path: Path) -> None:
    manifest = CRATE + '[lib]\npath = "lib/x.rs"\n[[bin]]\nname = "t"\npath = "tools/t.rs"\n'
    write(tmp_path, "Cargo.toml", manifest)
    files = RustProfile().build_context(tmp_path)
    assert "project/lib/x.rs" in files
    assert "project/tools/t.rs" in files


@pytest.mark.parametrize("path", ["../../etc/evil.rs", "/etc/evil.rs"])
def test_rust_stub_paths_cannot_leave_the_build_context(tmp_path: Path, path: str) -> None:
    write(tmp_path, "Cargo.toml", CRATE + f'[lib]\npath = "{path}"\n')
    assert all(
        ".." not in name and "/etc" not in name for name in RustProfile().build_context(tmp_path)
    )


def test_rust_workspace_root_needs_no_stubs(tmp_path: Path) -> None:
    write(tmp_path, "Cargo.toml", '[workspace]\nmembers = ["a"]\n')
    write(tmp_path, "a/Cargo.toml", CRATE)
    files = RustProfile().build_context(tmp_path)
    assert "project/src/lib.rs" not in files
    assert "project/a/src/lib.rs" in files


def test_invalid_cargo_manifest_does_not_crash(tmp_path: Path) -> None:
    write(tmp_path, "Cargo.toml", "not [valid")
    assert "project/Cargo.toml" in RustProfile().build_context(tmp_path)


@pytest.mark.parametrize("profile", [NodeProfile(), RustProfile()])
def test_symlinks_never_enter_the_build_context(tmp_path: Path, profile: object) -> None:
    secret = tmp_path / "outside" / "secret.txt"
    write(tmp_path, "outside/secret.txt", "TOP SECRET")
    repo = tmp_path / "repo"
    repo.mkdir()
    name = "package.json" if isinstance(profile, NodeProfile) else "Cargo.toml"
    (repo / name).symlink_to(secret)
    assert profile.build_context(repo) == {}  # type: ignore[attr-defined]


def test_python_requirements_symlink_is_not_read(tmp_path: Path) -> None:
    secret = write(tmp_path, "secret.txt", "leakedpackage\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").symlink_to(secret)
    assert PythonProfile().dependencies(repo) == ["pytest"]


@pytest.mark.parametrize(
    "path",
    ["test/a.js", "src/__tests__/a.js", "src/a.test.ts", "src/a.spec.tsx", "e2e/flow.ts"],
)
def test_node_test_paths(path: str) -> None:
    assert NodeProfile().is_test_path(path)


@pytest.mark.parametrize("path", ["src/a.ts", "src/testing.ts", "latest.js"])
def test_node_source_paths(path: str) -> None:
    assert not NodeProfile().is_test_path(path)


def test_rust_test_paths() -> None:
    assert RustProfile().is_test_path("cli/tests/flow.rs")
    assert not RustProfile().is_test_path("cli/src/lib.rs")
    assert not RustProfile().is_test_path("tests.rs")


def test_jest_and_vitest_failures_ignore_timing_and_color() -> None:
    first = "\x1b[31m FAIL \x1b[0m src/a.test.ts > adds numbers 12ms\n"
    second = " FAIL  src/a.test.ts > adds numbers 95ms\n"
    profile = NodeProfile()
    assert profile.failure_fingerprints(first) == profile.failure_fingerprints(second)
    assert profile.failure_fingerprints(first) == ["src/a.test.ts > adds numbers|assertion"]


def test_node_test_runner_failures() -> None:
    output = "ok 1 - good\nnot ok 2 - divides by zero\n"
    assert NodeProfile().failure_fingerprints(output) == ["divides by zero|assertion"]


def test_rust_failures() -> None:
    output = "test a::good ... ok\ntest a::bad ... FAILED\n"
    assert RustProfile().failure_fingerprints(output) == ["a::bad|assertion"]


def test_rust_compile_errors_have_fingerprints() -> None:
    output = "error[E0308]: mismatched types\n --> src/lib.rs:3:5\n"
    assert RustProfile().failure_fingerprints(output) == ["compile|E0308"]


def test_compiled_languages_get_more_room_than_python() -> None:
    assert RustProfile().test_timeout > PythonProfile().test_timeout
    assert RustProfile().max_seconds > PythonProfile().max_seconds


def test_github_language_names_cover_every_profile() -> None:
    assert {"Python", "JavaScript", "TypeScript", "Rust"} <= SUPPORTED_GITHUB_LANGUAGES


def test_remove_generated_cleans_nested_folders(tmp_path: Path) -> None:
    write(tmp_path, "node_modules/x/index.js")
    write(tmp_path, "packages/a/node_modules/y/index.js")
    keep = write(tmp_path, "packages/a/src/index.js")
    remove_generated(tmp_path, ("node_modules",))
    assert not (tmp_path / "node_modules").exists()
    assert not (tmp_path / "packages/a/node_modules").exists()
    assert keep.exists()


def test_remove_generated_never_follows_symlinks(tmp_path: Path) -> None:
    keep = write(tmp_path, "outside/important.txt", "keep")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "node_modules").symlink_to(tmp_path / "outside")
    remove_generated(repo, ("node_modules",))
    assert keep.exists()
    assert not (repo / "node_modules").exists()


def test_rust_failures_are_found_in_the_end_summary() -> None:
    output = "...cut...\nfailures:\n    a::one\n    b::two\n\ntest result: FAILED.\n"
    assert RustProfile().failure_fingerprints(output) == ["a::one|assertion", "b::two|assertion"]


def _runner(*results: CommandResult) -> tuple[SandboxTestRunner, FakeSandbox]:
    sandbox = FakeSandbox(*results)
    return SandboxTestRunner(sandbox, PythonProfile()), sandbox


FAIL_A = result(exit_code=1, stdout="FAILED t.py::a - ValueError: x\n")
FAIL_B = result(exit_code=1, stdout="FAILED t.py::b - ValueError: x\n")
FAIL_AB = result(exit_code=1, stdout="FAILED t.py::a - ValueError: x\nFAILED t.py::b - KeyError\n")


def test_clean_baseline_changes_nothing() -> None:
    runner, _ = _runner(result(), FAIL_A)
    runner.record_baseline()
    assert runner.known_failures == ()
    assert not runner.run().passed


def test_failure_that_existed_before_is_not_blamed_on_the_fix() -> None:
    runner, _ = _runner(FAIL_A, FAIL_A)
    runner.record_baseline()
    report = runner.run()
    assert report.passed
    assert report.exit_code == 1
    assert report.known_failures == ("t.py::a|ValueError",)
    assert "already failing" in report.output


def test_new_failure_is_still_caught_alongside_known_ones() -> None:
    runner, _ = _runner(FAIL_A, FAIL_AB)
    runner.record_baseline()
    report = runner.run()
    assert not report.passed
    assert "t.py::b|KeyError" in report.fingerprints


def test_replacing_one_failure_with_another_is_caught() -> None:
    runner, _ = _runner(FAIL_A, FAIL_B)
    runner.record_baseline()
    assert not runner.run().passed


def test_full_pass_after_a_failing_baseline() -> None:
    runner, _ = _runner(FAIL_A, result(stdout="all good"))
    runner.record_baseline()
    assert runner.run().passed


@pytest.mark.parametrize(
    "broken",
    [
        result(exit_code=2, stdout="no recognisable output"),
        result(exit_code=-1, timed_out=True),
    ],
)
def test_unidentifiable_failures_are_never_excused(broken: CommandResult) -> None:
    runner, _ = _runner(broken, broken)
    runner.record_baseline()
    assert runner.known_failures == ()
    assert not runner.run().passed


def test_compile_errors_are_never_excused() -> None:
    sandbox = FakeSandbox(
        result(exit_code=101, stderr="error[E0308]: mismatched\n"),
        result(exit_code=101, stderr="error[E0308]: mismatched\n"),
    )
    runner = SandboxTestRunner(sandbox, RustProfile())
    runner.record_baseline()
    assert not runner.run().passed


def test_rust_runs_every_test_program_even_after_a_failure() -> None:
    assert "--no-fail-fast" in RustProfile().test_command()


def test_a_hanging_rust_test_program_is_killed_by_the_runner() -> None:
    assert "RUNNER=" in RustProfile().dockerfile()
    assert "timeout" in RustProfile().dockerfile()


def test_rust_killed_test_program_has_a_fingerprint() -> None:
    output = "error: 2 targets failed:\n    `--bin agent-browser`\n    `--test page_headers_cli`\n"
    assert RustProfile().failure_fingerprints(output) == [
        "target:--bin agent-browser|failed",
        "target:--test page_headers_cli|failed",
    ]


def test_new_hanging_program_is_not_excused_by_earlier_failures() -> None:
    before = "failures:\n    a::one\n\nerror: 1 target failed:\n    `--bin app`\n"
    after = before.replace(
        "1 target failed:\n    `--bin app`", "2 targets failed:\n    `--bin app`\n    `--test new`"
    )
    sandbox = FakeSandbox(result(exit_code=101, stderr=before), result(exit_code=101, stderr=after))
    runner = SandboxTestRunner(sandbox, RustProfile())
    runner.record_baseline()
    report = runner.run()
    assert not report.passed
    assert "target:--test new|failed" in report.fingerprints


def test_failures_long_before_the_end_of_the_output_are_still_seen() -> None:
    noisy = "x" * 300_000 + "\ntest a::new ... FAILED\n" + "y" * 20_000
    sandbox = BigOutputSandbox(noisy)
    report = SandboxTestRunner(sandbox, RustProfile()).run()
    assert "a::new|assertion" in report.fingerprints


class BigOutputSandbox:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout

    def exec(self, argv: object, *, timeout: int, tail_chars: int = 8_000) -> CommandResult:
        return result(exit_code=101, stdout=self.stdout[-tail_chars:])
