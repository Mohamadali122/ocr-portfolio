"""The import-linter contract in pyproject.toml is a blocklist: it names three
concrete adapter modules `app.services` may not import. That catches every
adapter that existed when the contract was written, but silently misses a
fourth one added later -- nothing forces a new `app/ocr/whatever_engine.py` to
be added to the list.

This test closes that gap the cheap way: it globs `app/{ocr,storage,llm}/*.py`
and asserts every module there is accounted for, either in the contract's
`forbidden_modules` (a concrete adapter services must not import) or in
`_KNOWN_UTILITY_MODULES` below (a plain helper with no protocol to violate,
like `rasterize.py` -- a function library, not a class implementing
`OcrEngine`). Adding a new adapter file with neither forces a choice, not a
silent gap.
"""

import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Plain helpers in the adapter packages that are not themselves a protocol
# implementation -- nothing for `services` to accidentally couple to beyond
# what `app.ocr.base.OcrEngine` etc. already declare.
_KNOWN_UTILITY_MODULES = frozenset({"app.ocr.rasterize", "app.ocr.pdf_text"})


def _adapter_contract_forbidden_modules() -> set[str]:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    for contract in data["tool"]["importlinter"]["contracts"]:
        if contract["name"] == "Services depend on protocols, never on adapters":
            return set(contract["forbidden_modules"])
    raise AssertionError("contract not found in pyproject.toml")


def _adapter_package_modules() -> set[str]:
    modules = set()
    for package in ("ocr", "storage", "llm"):
        for path in (_REPO_ROOT / "app" / package).glob("*.py"):
            if path.stem in ("base", "__init__"):
                continue
            modules.add(f"app.{package}.{path.stem}")
    return modules


def test_every_adapter_module_is_named_in_the_contract_or_known_as_a_utility() -> None:
    forbidden = _adapter_contract_forbidden_modules()
    unaccounted = _adapter_package_modules() - forbidden - _KNOWN_UTILITY_MODULES
    assert not unaccounted, (
        f"{unaccounted} is new in app/ocr, app/storage, or app/llm and is neither "
        "in pyproject.toml's 'Services depend on protocols, never on adapters' "
        "forbidden_modules nor in this test's _KNOWN_UTILITY_MODULES. If it "
        "implements OcrEngine/LlmProvider/StorageBackend, add it to the contract. "
        "If it's a plain helper, add it to _KNOWN_UTILITY_MODULES."
    )


def test_known_utility_modules_do_not_implement_a_base_protocol() -> None:
    """Guards the other direction: something moved into the allowlist above
    must not actually be a swappable adapter, or the contract's whole point --
    services only see the protocol -- is quietly defeated."""
    sys.path.insert(0, str(_REPO_ROOT))
    try:
        for dotted in _KNOWN_UTILITY_MODULES:
            module = __import__(dotted, fromlist=["_"])
            # `vars()` includes names imported into the module (e.g. `rasterize.py`
            # importing `OcrError` to raise it), not just classes it defines --
            # filter to `__module__ == dotted` or an import for use-not-definition
            # would wrongly count as "this utility defines a class".
            classes = [
                v for v in vars(module).values() if isinstance(v, type) and v.__module__ == dotted
            ]
            assert not classes, (
                f"{dotted} defines a class ({classes}) but is listed as a plain "
                "utility, not a protocol adapter -- re-examine which list it belongs in."
            )
    finally:
        sys.path.remove(str(_REPO_ROOT))
