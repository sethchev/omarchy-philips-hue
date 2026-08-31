"""Theme hook lifecycle tests isolated from the user's Omarchy files."""
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "theme-sync" / "install.sh"
CLEANUP = ROOT / "cleanup.sh"
HOOK = ROOT / "theme-sync" / "45-hue.sh"


def paths(tmp_path):
    root = tmp_path / "sandbox"
    home = root / "home"
    return root, home, home / ".config/omarchy/hooks/theme-set.d/45-hue.sh", \
        home / ".config/omarchy/settings/hue-theme.json", \
        root / "state/omarchy/settings/omarchy-philips-hue-theme-hook.sha256"


def run(script, tmp_path, home=None):
    root, default_home, _, _, _ = paths(tmp_path)
    home = home or default_home
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "omarchy").write_text("""#!/bin/sh
case "$HOME" in "$TEST_ROOT"/*) ;; *) exit 97;; esac
target="$HOME/.config/omarchy/hooks/theme-set.d/$(basename "$4")"
case "$target" in "$TEST_ROOT"/*) ;; *) exit 98;; esac
if [ "$1" = hook ] && [ "$2" = install ] && [ "$3" = theme-set ]; then
  mkdir -p "$(dirname "$target")"
  cp "$4" "$target"
  chmod 755 "$target"
  printf '%s\\n' "$*" >> "$TEST_ROOT/omarchy-calls"
  exit 0
fi
exit 1
""")
    (bin_dir / "omarchy").chmod(0o755)
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(root / "xdg-config"),
           "XDG_STATE_HOME": str(root / "state"), "PATH": f"{bin_dir}:/usr/bin:/bin",
           "TEST_ROOT": str(root)}
    assert Path(env["HOME"]).resolve().is_relative_to(root.resolve())
    return subprocess.run(["bash", str(script)], text=True, capture_output=True, env=env)


def record_owner(path, owner):
    owner.parent.mkdir(parents=True, exist_ok=True)
    owner.write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")


def test_fresh_setup_uses_home_config_despite_xdg_override(tmp_path):
    root, _, hook, config, owner = paths(tmp_path)
    result = run(INSTALL, tmp_path)
    assert result.returncode == 0, result.stderr
    assert hook.read_bytes() == HOOK.read_bytes()
    assert hook.stat().st_mode & 0o777 == 0o755
    assert config.exists() and not (root / "xdg-config/omarchy/settings/hue-theme.json").exists()
    assert owner.exists()
    assert (root / "omarchy-calls").read_text().splitlines() == [f"hook install theme-set {HOOK}"]


def test_identical_hook_is_idempotent(tmp_path):
    root, _, hook, _, owner = paths(tmp_path)
    assert run(INSTALL, tmp_path).returncode == 0
    assert run(INSTALL, tmp_path).returncode == 0
    assert hook.read_bytes() == HOOK.read_bytes() and owner.exists()
    assert len((root / "omarchy-calls").read_text().splitlines()) == 1


def test_setup_preserves_existing_config_and_credentials(tmp_path):
    root, _, _, config, _ = paths(tmp_path)
    credentials = root / "state/omarchy/settings/hue.json"
    config.parent.mkdir(parents=True)
    credentials.parent.mkdir(parents=True)
    config.write_text('{"groups":["Bureau"]}')
    credentials.write_text('{"username":"secret"}')
    result = run(INSTALL, tmp_path)
    assert result.returncode == 0
    assert config.read_text() == '{"groups":["Bureau"]}'
    assert credentials.read_text() == '{"username":"secret"}'
    assert "secret" not in result.stdout + result.stderr


def test_spoofed_marker_and_symlink_collisions_are_refused(tmp_path):
    _, _, hook, _, _ = paths(tmp_path)
    hook.parent.mkdir(parents=True)
    hook.write_text('PLUGIN_DIR="$HOME/.config/omarchy/plugins/omarchy-philips-hue"\n')
    assert run(INSTALL, tmp_path).returncode == 1
    assert hook.exists()
    hook.unlink()
    target = tmp_path / "outside-hook"
    target.write_text("unrelated\n")
    hook.symlink_to(target)
    assert run(INSTALL, tmp_path).returncode == 1
    assert hook.is_symlink() and target.read_text() == "unrelated\n"


def test_owned_hook_can_upgrade_and_cleanup(tmp_path):
    _, _, hook, _, owner = paths(tmp_path)
    hook.parent.mkdir(parents=True)
    hook.write_text("old plugin hook\n")
    record_owner(hook, owner)
    assert run(INSTALL, tmp_path).returncode == 0
    assert hook.read_bytes() == HOOK.read_bytes()
    assert run(CLEANUP, tmp_path).returncode == 0
    assert not hook.exists() and not owner.exists()


def test_cleanup_leaves_unverified_and_symlink_hooks_untouched(tmp_path):
    _, _, hook, _, owner = paths(tmp_path)
    hook.parent.mkdir(parents=True)
    hook.write_text('PLUGIN_DIR="$HOME/.config/omarchy/plugins/omarchy-philips-hue"\n')
    assert run(CLEANUP, tmp_path).returncode == 0
    assert hook.exists()
    hook.unlink()
    target = tmp_path / "outside-hook"
    target.write_text("unrelated\n")
    hook.symlink_to(target)
    owner.parent.mkdir(parents=True, exist_ok=True)
    owner.write_text(hashlib.sha256(target.read_bytes()).hexdigest() + "\n")
    assert run(CLEANUP, tmp_path).returncode == 0
    assert hook.is_symlink() and target.read_text() == "unrelated\n" and owner.exists()


def test_cleanup_without_hook_is_harmless_and_isolated(tmp_path):
    root, home, hook, _, _ = paths(tmp_path)
    assert not hook.exists()
    assert run(CLEANUP, tmp_path).returncode == 0
    assert not hook.exists() and home.resolve().is_relative_to(root.resolve())


def test_test_runner_rejects_a_home_outside_its_temporary_root(tmp_path):
    with pytest.raises(AssertionError):
        run(INSTALL, tmp_path, tmp_path.parent / "outside-home")


def test_pairing_uses_the_existing_hook_installer():
    assert 'bash "$HERE/theme-sync/install.sh"' in (ROOT / "pair.sh").read_text()
