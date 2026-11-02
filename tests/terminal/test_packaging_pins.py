"""The terminal archive's pins: the interpreter digests, the hash-pinned wheel locks, and the launchers that prefer the
bundled runtime. Offline: nothing is downloaded (the build and the smoke test do that)."""

import re
import sys
import tempfile
import unittest
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))

import lock_wheels as lw  # noqa: E402
import package_terminal as pt  # noqa: E402


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pins(text: str) -> dict[str, list[tuple[str, object]]]:
    """The pins in a requirements file, read the way packaging/lock_wheels.py reads the same lines (#4526).

    `packaging.requirements.Requirement` keeps an extras pin (`psycopg[binary]==3.3.6`) and the environment marker;
    the regex this check used before matched only a plain name and could not evaluate a marker at all."""
    pins: dict[str, list[tuple[str, object]]] = {}
    for line in text.splitlines():
        line = line.split(" #")[0].strip()
        if not line or line.startswith(("#", "-")):      # comments and pip options (--index-url ...) are not pins
            continue
        req = Requirement(line)
        version = next((s.version for s in req.specifier if s.operator == "=="), None)
        if version is None:
            continue
        pins.setdefault(norm(req.name), []).append((version, req.marker))
    return pins


def requirement_pins() -> dict[str, list[tuple[str, object]]]:
    """The direct runtime pins of requirements.txt and requirements-monitor.txt."""
    pins: dict[str, list[tuple[str, object]]] = {}
    for name in ("requirements.txt", "requirements-monitor.txt"):
        for pkg, versions in parse_pins((ROOT / name).read_text(encoding="utf-8")).items():
            pins.setdefault(pkg, []).extend(versions)
    return pins


def pins_for_target(pins: dict[str, list[tuple[str, object]]], target: str) -> dict[str, str]:
    """{package: version} a lock for `target` must carry: a pin whose marker is false for the target is not demanded.

    The marker is evaluated in the environment lock_wheels.py builds the lock with, so this check cannot drift from
    the tool that made the locks (a Windows-only pin belongs in the Windows lock only, and a Linux-only one must not
    be demanded there)."""
    env = {**lw.TARGETS[target]["env"], "python_version": lw.PYTHON, "python_full_version": lw.PYTHON + ".15",
           "implementation_name": "cpython", "platform_python_implementation": "CPython"}
    out = {}
    for pkg, versions in pins.items():
        applicable = [version for version, marker in versions if marker is None or marker.evaluate(env)]
        if applicable:
            out[pkg] = applicable[0]
    return out


class InterpreterPins(unittest.TestCase):
    def test_every_target_has_a_digest_and_a_matching_url(self):
        self.assertEqual(sorted(pt.PYTHON_PINS), ["darwin-arm64", "linux-aarch64", "linux-x86_64", "windows-x86_64"])
        for target, pin in pt.PYTHON_PINS.items():
            with self.subTest(target):
                self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
                self.assertIn(pt.PYTHON_VERSION, pin["url"])
                self.assertIn(pt.PBS_RELEASE, pin["url"])
                self.assertTrue(pin["url"].startswith("https://github.com/astral-sh/python-build-standalone/"))
                self.assertTrue(pin["url"].endswith("install_only.tar.gz"))
        self.assertEqual(len({p["sha256"] for p in pt.PYTHON_PINS.values()}), 4)

    def test_the_mac_pin_is_the_one_setup_sh_downloads(self):
        text = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn(pt.PYTHON_PINS["darwin-arm64"]["sha256"], text)
        self.assertIn(pt.PYTHON_PINS["darwin-arm64"]["url"].split("/")[-1], text)

    def test_fetch_refuses_a_download_that_does_not_match_its_pin(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            pin = pt.PYTHON_PINS["linux-x86_64"]
            (cache / pin["file"].replace("+", "_")).write_bytes(b"not the interpreter")
            with self.assertRaises(SystemExit) as cm:
                pt.fetch_python("linux-x86_64", cache)
            self.assertIn("does not match its pin", str(cm.exception))


class WheelLocks(unittest.TestCase):
    def locks(self):
        found = sorted((ROOT / "packaging" / "locks").glob("runtime-*-cp312.txt"))
        self.assertEqual([p.name for p in found], ["runtime-darwin-arm64-cp312.txt", "runtime-linux-aarch64-cp312.txt",
                                                    "runtime-linux-x86_64-cp312.txt", "runtime-windows-x86_64-cp312.txt"])
        return found

    def test_every_entry_is_pinned_and_hashed_and_nothing_else_is_in_the_file(self):
        for lock in self.locks():
            with self.subTest(lock.name):
                entries = {}
                current = None
                for line in lock.read_text(encoding="utf-8").splitlines():
                    if not line.strip() or line.startswith("#"):
                        continue
                    m = re.match(r"^([A-Za-z0-9_.-]+)==([0-9][^\s\\]*) \\$", line)
                    if m:
                        current = norm(m.group(1))
                        entries[current] = {"version": m.group(2), "hashes": []}
                        continue
                    h = re.match(r"^\s+--hash=sha256:([0-9a-f]{64})( \\)?$", line)
                    self.assertTrue(h and current, f"unexpected line in {lock.name}: {line!r}")
                    entries[current]["hashes"].append(h.group(1))
                self.assertGreater(len(entries), 15)
                for name, e in entries.items():
                    self.assertTrue(e["hashes"], name)

    def test_the_locks_agree_with_requirements_txt(self):
        pins = requirement_pins()
        self.assertTrue(pins)                                # the parser found the direct pins at all
        for lock in self.locks():
            target = lock.name[len("runtime-"):-len("-cp312.txt")]
            names = pt.lock_names(lock)
            with self.subTest(lock.name):
                for pkg, version in pins_for_target(pins, target).items():
                    self.assertEqual(names.get(pkg), version, pkg)
                self.assertNotIn("cmake", names)             # build tools never ship in the bundle
                self.assertNotIn("ninja", names)

    def test_the_build_installs_with_hash_checking_and_no_resolution(self):
        text = (ROOT / "packaging" / "package_terminal.py").read_text(encoding="utf-8")
        for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:"):
            self.assertIn(flag, text)
        self.assertNotIn('"--upgrade"', text)               # pip itself is the interpreter's, not a fresh download


class RequirementPins(unittest.TestCase):
    """#4526: the agreement check reads the pins and their markers the way packaging/lock_wheels.py does."""

    def test_an_extras_pin_is_read(self):
        # `psycopg[binary]==3.3.6`: the old regex `^([A-Za-z0-9_.-]+)==...` matched nothing, so a lock that dropped
        # the package still passed the check.
        pins = parse_pins("psycopg[binary]==3.3.6\n")
        self.assertEqual([version for version, _ in pins["psycopg"]], ["3.3.6"])

    def test_a_windows_only_pin_is_not_demanded_in_another_lock(self):
        pins = parse_pins('colorama==0.4.6; sys_platform == "win32"\n')
        self.assertEqual(pins_for_target(pins, "linux-x86_64"), {})
        self.assertEqual(pins_for_target(pins, "windows-x86_64"), {"colorama": "0.4.6"})

    def test_a_linux_only_pin_is_not_demanded_in_the_windows_lock(self):
        pins = parse_pins('embedded-postgres==18.6.3; sys_platform != "win32"\n')
        self.assertEqual(pins_for_target(pins, "windows-x86_64"), {})
        self.assertEqual(pins_for_target(pins, "linux-x86_64"), {"embedded-postgres": "18.6.3"})


class Launchers(unittest.TestCase):
    def test_wrappers_prefer_the_bundled_runtime(self):
        cmd = (ROOT / "bin" / "strata.cmd").read_text(encoding="utf-8")
        ps1 = (ROOT / "bin" / "strata.ps1").read_text(encoding="utf-8")
        sh = (ROOT / "bin" / "strata").read_text(encoding="utf-8")
        self.assertLess(cmd.index("python\\python.exe"), cmd.index("py -3"))
        self.assertLess(ps1.index("python\\python.exe"), ps1.index("'py', '-3'"))
        self.assertLess(sh.index("python/bin/python3"), sh.index("command -v python3"))
        for text in (cmd, ps1, sh):
            self.assertIn("PYTHONNOUSERSITE", text)

    def test_start_here_setup_and_update_use_it(self):
        bat = (ROOT / "START-HERE.bat").read_text(encoding="utf-8")
        self.assertLess(bat.index('if exist "python\\python.exe" goto bundled'), bat.index("call :findpy"))
        self.assertIn("START-HERE.bat", (ROOT / "SETUP.bat").read_text(encoding="utf-8"))
        self.assertIn("START-HERE.bat", (ROOT / "UPDATE.bat").read_text(encoding="utf-8"))
        sh = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertLess(sh.index("exec python/bin/python3"), sh.index("if [ ! -x .venv/bin/python ]"))
        self.assertIn("setup.sh", (ROOT / "update.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
