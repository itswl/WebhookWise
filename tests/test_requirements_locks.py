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


def _declared_package_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        name = stripped.split(";", 1)[0]
        for sep in ("[", "==", ">=", "<=", "~=", "!=", ">", "<"):
            name = name.split(sep, 1)[0]
        normalized = name.strip().lower().replace("_", "-").replace(".", "-")
        if normalized:
            names.add(normalized)
    return names


def test_runtime_and_dev_locks_do_not_pin_conflicting_versions() -> None:
    runtime = _locked_versions(PROJECT_ROOT / "requirements.lock")
    dev = _locked_versions(PROJECT_ROOT / "requirements-dev.lock")

    conflicts = {
        name: (runtime[name], dev[name]) for name in sorted(runtime.keys() & dev.keys()) if runtime[name] != dev[name]
    }

    assert conflicts == {}


def test_dev_lock_contains_all_direct_dev_requirements() -> None:
    declared = _declared_package_names(PROJECT_ROOT / "requirements-dev.txt")
    locked = set(_locked_versions(PROJECT_ROOT / "requirements-dev.lock"))

    assert declared - locked == set()


def test_uv_lock_is_not_used_without_project_metadata() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    has_project_metadata = "\n[project]\n" in f"\n{pyproject}\n"

    assert not (PROJECT_ROOT / "uv.lock").exists() or has_project_metadata
