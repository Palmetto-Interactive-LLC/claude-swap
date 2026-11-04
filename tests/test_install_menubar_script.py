"""The install script's flow, against stub tools. Nothing here installs anything."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-menubar.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class _Stubs:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path_dir = root / "path"
        self.tool_dir = root / "tools"
        self.bin_dir = root / "bin"
        self.calls = root / "calls.log"
        self.home = root / "home"
        # Never the real one: --uninstall deletes this path.
        self.app_bundle = root / "claude-swap Menubar.app"
        for directory in (self.path_dir, self.tool_dir / "claude-swap" / "bin", self.bin_dir, self.home):
            directory.mkdir(parents=True)
        self.calls.touch()
        self.service_status = "Menu bar service: running (pid 42)"
        self.make_cswap = True
        self.appkit_exit = 0
        self.uname = "Darwin"

    def write(self) -> None:
        log = f'echo "$(basename "$0") $*" >> "{self.calls}"'
        _stub(self.path_dir, "uname", f'echo "{self.uname}"')
        _stub(self.path_dir, "sleep", "exit 0")
        _stub(self.path_dir, "claude", "exit 0")
        cswap = self.bin_dir / "cswap"
        make = (
            f"""cat > "{cswap}" <<'CSWAP'
#!/bin/bash
echo "$(basename "$0") $*" >> "{self.calls}"
case "$*" in
  --version) echo "cswap 1.2.3" ;;
  "menubar --service-status") echo "{self.service_status}" ;;
esac
CSWAP
chmod +x "{cswap}" """
            if self.make_cswap
            else "true"
        )
        _stub(
            self.path_dir,
            "uv",
            f"""{log}
case "$*" in
  "tool dir --bin") echo "{self.bin_dir}" ;;
  "tool dir") echo "{self.tool_dir}" ;;
  "tool list") echo "claude-swap v1.2.3" ;;
  tool\\ install*) {make} ;;
esac""",
        )
        _stub(
            self.tool_dir / "claude-swap" / "bin",
            "python",
            f"""case "$2" in
  "import AppKit") exit {self.appkit_exit} ;;
  *) echo "" ;;
esac""",
        )

    def run(self, *args: str, path_extra: str = "") -> subprocess.CompletedProcess[str]:
        self.write()
        env = {
            "HOME": str(self.home),
            "CSWAP_APP_BUNDLE": str(self.app_bundle),
            "PATH": os.pathsep.join(
                filter(None, [str(self.path_dir), str(self.bin_dir), path_extra, "/usr/bin", "/bin"])
            ),
        }
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False
        )

    @property
    def logged(self) -> list[str]:
        return self.calls.read_text().splitlines()


@pytest.fixture
def stubs(tmp_path: Path) -> _Stubs:
    return _Stubs(tmp_path)


def test_installs_the_fork_with_a_managed_python_and_starts_the_service(stubs: _Stubs) -> None:
    result = stubs.run()

    assert result.returncode == 0, result.stderr
    install = next(line for line in stubs.logged if line.startswith("uv tool install"))
    assert "--managed-python" in install
    assert "--python 3.13" in install
    assert "claude-swap[menubar] @ git+https://github.com/Palmetto-Interactive-LLC/claude-swap@main" in install
    assert "cswap menubar --install-service" in stubs.logged
    assert "running (pid 42)" in result.stdout
    assert "cswap add" in result.stdout


def test_ref_picks_what_gets_installed(stubs: _Stubs) -> None:
    assert stubs.run("--ref", "some-branch").returncode == 0

    assert any("claude-swap@some-branch" in line for line in stubs.logged)


def test_local_installs_this_checkout(stubs: _Stubs) -> None:
    assert stubs.run("--local", "--no-service").returncode == 0

    assert any(f"@ file://{SCRIPT.parent.parent}" in line for line in stubs.logged)


def test_no_service_leaves_launchd_alone(stubs: _Stubs) -> None:
    result = stubs.run("--no-service")

    assert result.returncode == 0, result.stderr
    assert not any("--install-service" in line for line in stubs.logged)
    assert "Look for the icon" not in result.stdout
    assert 'Run "cswap menubar"' in result.stdout


def test_refuses_anything_but_macos(stubs: _Stubs) -> None:
    stubs.uname = "Linux"

    result = stubs.run()

    assert result.returncode == 1
    assert "macOS only" in result.stderr
    assert stubs.logged == []


def test_fails_loudly_when_appkit_will_not_load(stubs: _Stubs) -> None:
    stubs.appkit_exit = 1

    result = stubs.run()

    assert result.returncode == 1
    assert "AppKit" in result.stderr
    assert not any("--install-service" in line for line in stubs.logged)


def test_fails_when_the_service_does_not_come_up(stubs: _Stubs) -> None:
    stubs.service_status = "Menu bar service is not installed."

    result = stubs.run()

    assert result.returncode == 1
    assert "didn't start" in result.stderr


def test_fails_when_the_install_leaves_no_cswap(stubs: _Stubs) -> None:
    stubs.make_cswap = False

    result = stubs.run()

    assert result.returncode == 1
    assert "is missing" in result.stderr


def test_warns_when_another_cswap_comes_first_on_path(stubs: _Stubs, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _stub(other, "cswap", "exit 0")
    shutil.move(str(other / "cswap"), str(stubs.path_dir / "cswap"))

    result = stubs.run("--no-service")

    assert result.returncode == 0
    assert "Remove the older install" in result.stderr


def test_uninstall_removes_the_service_and_tool_and_keeps_accounts(stubs: _Stubs) -> None:
    assert stubs.run("--no-service").returncode == 0
    stubs.app_bundle.mkdir()

    result = stubs.run("--uninstall")

    assert result.returncode == 0, result.stderr
    assert not stubs.app_bundle.exists()
    assert "cswap menubar --uninstall-service" in stubs.logged
    assert "uv tool uninstall claude-swap" in stubs.logged
    assert "accounts are still" in result.stdout


def test_unknown_option_is_an_error(stubs: _Stubs) -> None:
    result = stubs.run("--nope")

    assert result.returncode == 1
    assert "unknown option" in result.stderr


def test_help_prints_the_usage_without_touching_anything(stubs: _Stubs) -> None:
    result = stubs.run("--help")

    assert result.returncode == 0
    assert "--uninstall" in result.stdout
    assert stubs.logged == []


def _piped(stubs: _Stubs, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script the way curl | bash does: from stdin, with no script file."""
    stubs.write()
    env = {
        "HOME": str(stubs.home),
        "CSWAP_APP_BUNDLE": str(stubs.app_bundle),
        "PATH": os.pathsep.join([str(stubs.path_dir), str(stubs.bin_dir), "/usr/bin", "/bin"]),
    }
    return subprocess.run(
        ["/bin/bash", "-s", "--", *args],
        input=SCRIPT.read_text(),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_piped_install_runs_end_to_end(stubs: _Stubs) -> None:
    result = _piped(stubs)

    assert result.returncode == 0, result.stderr
    assert "cswap menubar --install-service" in stubs.logged


def test_piped_help_works_without_a_script_file(stubs: _Stubs) -> None:
    result = _piped(stubs, "--help")

    assert result.returncode == 0, result.stderr
    assert "--uninstall" in result.stdout


def test_piped_local_explains_that_it_needs_a_checkout(stubs: _Stubs) -> None:
    result = _piped(stubs, "--local")

    assert result.returncode == 1
    assert "has to run from a checkout" in result.stderr
    assert stubs.logged == []
