"""The R11 review gate. Loads reviewed custom modules — and by default, nothing.

**Off at launch (decision D5).** `load_modules` returns an empty list unless
`SCANNER_CUSTOM_MODULES_ENABLED=true`, so shipping this file changes no behaviour.
It exists so the contract and its gate are reviewable *before* anyone generates code
against them.

## The thing to understand before changing any of this

**Importing a Python module executes it.** There is no "load and inspect without
running" — module-level code runs at import, before any interface check can happen.
So every gate here is necessarily *pre-import*, and there is **no sandbox**: a module
that passes review runs with the full privileges of the scan process, which holds
database credentials and an API key.

That makes the review record the only real control, and it makes the checksum
load-bearing rather than hygienic: without binding reviewed bytes to loaded bytes,
"reviewed" refers to whatever the file said at review time, which may not be what is
about to run.

## What a module spec must carry

    {
      "name": "capital_campaign_detector",
      "path": "modules/capital_campaign_detector.py",
      "sha256": "<hex digest of the file as reviewed>",
      "api_version": "1.0",
      "reviewed_by": "<user id or email>",
      "reviewed_at": "2026-08-04T00:00:00Z"
    }

⚠️ **There is nowhere in the ratified config for this yet.** The skill-builder config
schema has root `additionalProperties: false` and no `custom_modules` key, so a config
carrying modules is rejected at finalize today. Enabling R11 therefore needs a
coordinated schema bump, not a free additive change — the closed root was chosen
precisely so additions are deliberate. Flagged rather than worked around.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
from typing import Any

from aeo.modules.interface import MODULE_API_VERSION, CustomModule

#: Fields a spec must carry before the file is even hashed, let alone imported.
REQUIRED_SPEC_FIELDS = ("name", "path", "sha256", "api_version", "reviewed_by", "reviewed_at")


class ModuleRejected(Exception):
    """A module failed the gate. Carries why, for the audit trail."""

    def __init__(self, name: str, reason: str) -> None:
        self.module_name = name
        self.reason = reason
        super().__init__(f"custom module {name!r} rejected: {reason}")


def modules_enabled() -> bool:
    """Whether R11 execution is switched on. **False at launch (D5).**

    Read per call rather than captured at import so a spec can flip it without
    re-importing the world — the same reasoning as aeo-backend's
    `isGroundTruthEnabled()`.
    """
    return os.environ.get("SCANNER_CUSTOM_MODULES_ENABLED") == "true"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_spec(spec: dict[str, Any], *, base_dir: Path) -> Path:
    """Check a spec and return the resolved file path. Raises `ModuleRejected`.

    Every check here happens **before import**, because import is execution.
    """
    name = str(spec.get("name") or "<unnamed>")

    missing = [f for f in REQUIRED_SPEC_FIELDS if not spec.get(f)]
    if missing:
        raise ModuleRejected(name, f"spec is missing {', '.join(missing)}")

    if spec["api_version"] != MODULE_API_VERSION:
        # A contract change invalidates prior reviews: the reviewer approved code
        # against different expectations.
        raise ModuleRejected(
            name,
            f"reviewed against module API {spec['api_version']}, runtime is "
            f"{MODULE_API_VERSION} — needs re-review",
        )

    # Path containment. A spec is data that reached us over the wire; without this a
    # `path` of `../../etc/anything` or an absolute path would be imported.
    raw_path = str(spec["path"])
    resolved = (base_dir / raw_path).resolve()
    if not str(resolved).startswith(str(base_dir.resolve())):
        raise ModuleRejected(name, f"path {raw_path!r} escapes the module directory")
    if not resolved.is_file():
        raise ModuleRejected(name, f"file {raw_path!r} not found")

    actual = _digest(resolved)
    if actual != spec["sha256"]:
        # The load-bearing check: without it, "reviewed" refers to bytes that may
        # since have changed.
        raise ModuleRejected(
            name, f"checksum mismatch — reviewed {spec['sha256'][:12]}…, on disk {actual[:12]}…"
        )

    return resolved


def validate_module(candidate: Any, *, expected_name: str) -> CustomModule:
    """Check an imported object satisfies the interface. Raises `ModuleRejected`."""
    if not isinstance(candidate, CustomModule):
        raise ModuleRejected(expected_name, "does not expose name/api_version/signals")
    if not callable(getattr(candidate, "signals", None)):
        # `runtime_checkable` only checks attribute presence, not that it is callable.
        raise ModuleRejected(expected_name, "`signals` is not callable")
    if getattr(candidate, "name", None) != expected_name:
        raise ModuleRejected(
            expected_name,
            f"declares name {getattr(candidate, 'name', None)!r}, spec says "
            f"{expected_name!r} — signals would be namespaced under the wrong module",
        )
    return candidate


def load_modules(
    specs: list[dict[str, Any]] | None,
    *,
    base_dir: Path,
    on_reject: Any = None,
) -> list[CustomModule]:
    """Load every spec that passes the gate. Returns `[]` when R11 is off.

    A rejected module never blocks the scan — it is reported via `on_reject` and
    skipped. A scan that refuses to run because generated code was stale would be a
    worse outcome than one that runs without an optional signal.
    """
    if not specs:
        return []
    if not modules_enabled():
        if on_reject:
            on_reject(
                "<all>",
                f"{len(specs)} custom module(s) declared but R11 is off "
                f"(SCANNER_CUSTOM_MODULES_ENABLED is not 'true')",
            )
        return []

    loaded: list[CustomModule] = []
    for spec in specs:
        name = str(spec.get("name") or "<unnamed>")
        try:
            path = validate_spec(spec, base_dir=base_dir)

            # ⚠️ Everything above this line is the security boundary. The next two
            # statements execute the module's top level.
            module_spec = importlib.util.spec_from_file_location(f"custom_{name}", path)
            if module_spec is None or module_spec.loader is None:
                raise ModuleRejected(name, "could not be loaded as a Python module")
            imported = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(imported)

            candidate = getattr(imported, "MODULE", None)
            if candidate is None:
                raise ModuleRejected(name, "does not define a module-level `MODULE`")
            loaded.append(validate_module(candidate, expected_name=name))
        except ModuleRejected as exc:
            if on_reject:
                on_reject(exc.module_name, exc.reason)
        except Exception as exc:  # noqa: BLE001 — generated code; anything can happen
            if on_reject:
                on_reject(name, f"failed to import: {type(exc).__name__}: {exc}")
    return loaded
