"""Explicit collaborator for tests of behavior *after* watchdog admission.

The real guard and inherited-lock/activation protocol are exercised separately
by test_native_admission, test_runtime_admission and test_unit_admission. This
file is never packaged or consulted by production resource discovery.
"""

from pathlib import Path


def admit_watchdog_fixture(libdir: Path) -> Path:
    """Replace only the caller-owned fixture guard; record each expected call."""
    guard = libdir / "runtime-admission.sh"
    if guard.is_symlink():
        guard.unlink()
    guard.write_text('''# Authored test collaborator: watchdog admission already tested.
native_admission() {
    [ "$#" -eq 1 ] && [ "$1" = keepalive ] || return 7
    printf '%s\\n' "$1" >> "$LIB_DIR/admission-fixture.calls"
}
''')
    return libdir / "admission-fixture.calls"
