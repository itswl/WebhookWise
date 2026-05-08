from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "==" not in stripped:
            continue
        name, version = stripped.split("==", 1)
        normalized = name.lower().replace("_", "-").replace(".", "-")
        versions[normalized] = version.split(";", 1)[0].strip()
    return versions


def test_runtime_and_dev_locks_do_not_pin_conflicting_versions() -> None:
    runtime = _locked_versions(PROJECT_ROOT / "requirements.lock")
    dev = _locked_versions(PROJECT_ROOT / "requirements-dev.lock")

    conflicts = {
        name: (runtime[name], dev[name]) for name in sorted(runtime.keys() & dev.keys()) if runtime[name] != dev[name]
    }

    assert conflicts == {}
