"""`TAGS.docx`-derived tag registry, joined against `tag_rules.toml`.

`TAGS.docx` is prebilling's single source of truth for which tags exist and
why: a two-column table, tag name | reason, one row per tag family. Two real
shapes in that table are handled here rather than by editing the source
document: a cell can hold more than one tag name, one per paragraph line,
sharing that row's one reason (e.g. "Invalid Pt Demos" / "Do Not Bill Pt" /
"FS Request" all share one CBO-address reason); and a name can repeat across
different rows with a different reason each time (e.g. "Do Not Bill Pt" also
appears with an unrelated HCA-group reason) -- those reasons are concatenated,
not overwritten, so nothing the SOP says is silently dropped.

`tag_rules.toml` is the machine-readable half: a `[[tag]]` entry per tag whose
REASON prose is specific enough to compile into a predicate (see
`app/services/tagging.py` for the leaf vocabulary the predicate tree is built
from). A docx tag with no matching TOML entry is a manual-review prompt: it
is surfaced to a reviewer with its REASON text and never auto-assigned --
`files_for_project/PREBILLER_QUESTIONS.md` catalogues why several of them (MCR Age, MCD Minor,
...) are withheld this way even though they look rulable at a glance.

`load_registry()` is called once from `app/main.py`'s lifespan, so a
malformed docx or a TOML typo (a rule naming a tag the docx doesn't have)
fails application startup, not a random request mid-shift.
"""

import re
import tomllib
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from app.models.document import DocumentType
from app.schemas.extraction import FIELD_NAMES, INSURANCE_FIELD_NAMES, MAX_INSURANCE_SLOTS

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# The registry's join key: casefold + collapsed whitespace. Two names differing
# only in case or a stray double space (the docx has both -- "Port Arthur  Quick
# bill" carries a doubled space) must resolve to the same tag.
_WHITESPACE = re.compile(r"\s+")


def key(name: str) -> str:
    return _WHITESPACE.sub(" ", name).strip().casefold()


_TOP_KEYS = (
    "all",
    "any",
    "none",
    "text",
    "field",
    "insurance",
    "has_doc_type",
    "missing_doc_type",
    "field_conflict",
    "numeric_diff",
    "requires_tag",
)


class RegistryError(ValueError):
    """A malformed `TAGS.docx` or `tag_rules.toml`. Raised at startup, not per-request."""


@dataclass(frozen=True, slots=True)
class TagRule:
    rule_id: str  # the tag name as printed in tag_rules.toml -- stored on ClaimTag.rule_id
    tree: dict[str, Any]  # predicate tree, evaluated by app.services.tagging._eval_node


@dataclass(frozen=True, slots=True)
class TagDefinition:
    name: str  # as printed in the docx (first occurrence, if the key repeats)
    key: str
    reason: str  # docx REASON prose, shown to the reviewer verbatim
    rule: TagRule | None = None  # None => manual-review prompt, no auto-assignment


@dataclass(frozen=True, slots=True)
class Registry:
    tags: tuple[TagDefinition, ...]  # docx row order -- app.services.tagging relies on this

    def by_key(self, name: str) -> TagDefinition | None:
        target = key(name)
        for tag in self.tags:
            if tag.key == target:
                return tag
        return None

    def has(self, name: str) -> bool:
        return self.by_key(name) is not None

    @property
    def rulable(self) -> tuple[TagDefinition, ...]:
        return tuple(t for t in self.tags if t.rule is not None)

    @property
    def manual_review(self) -> tuple[TagDefinition, ...]:
        return tuple(t for t in self.tags if t.rule is None)


def _cell_text_lines(tc: ET.Element) -> list[str]:
    """One string per paragraph (`<w:p>`) in a table cell -- a cell holding
    several tag names has one name per paragraph, and the docx's own line
    breaks are exactly the boundary that separates them."""
    lines = []
    for p in tc.findall(f"{_W_NS}p"):
        lines.append("".join(t.text or "" for t in p.findall(f".//{_W_NS}t")))
    return lines


def _parse_docx(path: Path) -> list[tuple[str, str]]:
    """-> `[(name, reason), ...]`, one entry per tag name. A cell holding
    several names on separate paragraph lines yields one entry per name, all
    sharing that row's reason. Skips the header row and the one fully blank
    separator row the real file has."""
    if not path.is_file():
        raise RegistryError(
            f"{path} is not a file — check the TAGS.docx bind mount in docker-compose.yml"
        )
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise RegistryError(f"cannot read {path} as a .docx: {exc}") from exc

    # S314: xml.etree is unsafe for attacker-controlled XML (XXE, billion
    # laughs). TAGS.docx is not attacker-controlled -- it's a tracked file
    # under `files_for_project/` the prebilling team edits directly and that
    # ships in the read-only
    # `./files_for_project/TAGS.docx:/srv/app/TAGS.docx:ro` mount, the same
    # trust level as
    # `tag_rules.toml` a few lines below (parsed with stdlib `tomllib`, no
    # equivalent warning because TOML has no entity-expansion attack surface).
    # Not worth a `defusedxml` dependency for a file with no untrusted path in.
    root = ET.fromstring(xml)  # noqa: S314
    tables = root.findall(f".//{_W_NS}tbl")
    if not tables:
        raise RegistryError(f"{path}: no table found")
    rows = tables[0].findall(f"{_W_NS}tr")

    entries: list[tuple[str, str]] = []
    for row_index, row in enumerate(rows):
        if row_index == 0:
            continue  # header: "TAGS" | "REASON"
        cells = row.findall(f"{_W_NS}tc")
        if len(cells) < 2:
            continue
        names = [line.strip() for line in _cell_text_lines(cells[0]) if line.strip()]
        reason_lines = [line.strip() for line in _cell_text_lines(cells[1]) if line.strip()]
        if not names:
            continue  # the blank separator row
        reason = "\n".join(reason_lines)
        entries.extend((name, reason) for name in names)
    return entries


def _merge(entries: list[tuple[str, str]]) -> tuple[TagDefinition, ...]:
    """Fold repeated names (same `key()`) into one `TagDefinition`, concatenating
    a second occurrence's reason onto the first's rather than discarding it --
    the two occurrences describe genuinely different SOP conditions under the
    same tag name (e.g. "Do Not Bill Pt"), and neither should be silently lost."""
    order: list[str] = []
    by_key: dict[str, TagDefinition] = {}
    for name, reason in entries:
        k = key(name)
        existing = by_key.get(k)
        if existing is None:
            order.append(k)
            by_key[k] = TagDefinition(name=name, key=k, reason=reason)
        elif reason and reason not in existing.reason.split("\n\n"):
            by_key[k] = TagDefinition(
                name=existing.name,
                key=k,
                reason=f"{existing.reason}\n\n{reason}" if existing.reason else reason,
            )
    return tuple(by_key[k] for k in order)


def _load_rules(path: Path) -> dict[str, TagRule]:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except OSError as exc:
        raise RegistryError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{path}: invalid TOML: {exc}") from exc

    rules: dict[str, TagRule] = {}
    for entry in data.get("tag", []):
        name = entry.get("name")
        if not name:
            raise RegistryError(f"{path}: a [[tag]] entry is missing `name`")
        k = key(name)
        if k in rules:
            raise RegistryError(f"{path}: duplicate rule for tag {name!r}")
        tree = {k2: v for k2, v in entry.items() if k2 != "name"}
        if not tree:
            raise RegistryError(f"{path}: tag {name!r} has no rule condition")
        rules[k] = TagRule(rule_id=name, tree=tree)
    return rules


def _validate_predicate_tree(
    tree: dict[str, Any],
    *,
    rules_path: Path,
    tag_name: str,
    tag_order: dict[str, int],
) -> None:
    keys = [k for k in _TOP_KEYS if k in tree]
    if len(keys) != 1:
        raise RegistryError(
            f"{rules_path}: tag {tag_name!r} has a node with keys {list(tree)}; "
            "each node must have exactly one condition key"
        )
    key_name = keys[0]

    if key_name in ("all", "any", "none"):
        for child in tree[key_name]:
            _validate_predicate_tree(
                child, rules_path=rules_path, tag_name=tag_name, tag_order=tag_order
            )
        return

    if key_name == "field":
        field_name = tree["field"]
        if field_name not in FIELD_NAMES:
            raise RegistryError(
                f"{rules_path}: tag {tag_name!r} references unknown field {field_name!r}"
            )
        if pattern := tree.get("matches"):
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise RegistryError(
                    f"{rules_path}: tag {tag_name!r} field {field_name!r} "
                    f"has invalid regex {pattern!r}: {exc}"
                ) from exc
        return

    if key_name == "field_conflict":
        field_name = tree["field_conflict"]
        if field_name not in FIELD_NAMES:
            raise RegistryError(
                f"{rules_path}: tag {tag_name!r} references unknown field_conflict "
                f"{field_name!r}"
            )
        return

    if key_name == "insurance":
        spec = tree["insurance"]
        slot = int(spec["slot"])
        if slot < 0 or slot >= MAX_INSURANCE_SLOTS:
            raise RegistryError(
                f"{rules_path}: tag {tag_name!r} insurance slot {slot} out of range"
            )
        field_name = spec["field"]
        if field_name not in INSURANCE_FIELD_NAMES:
            raise RegistryError(
                f"{rules_path}: tag {tag_name!r} references unknown insurance field "
                f"{field_name!r}"
            )
        if pattern := spec.get("matches"):
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise RegistryError(
                    f"{rules_path}: tag {tag_name!r} insurance field {field_name!r} "
                    f"has invalid regex {pattern!r}: {exc}"
                ) from exc
        return

    if key_name in ("has_doc_type", "missing_doc_type"):
        doc_type_name = tree[key_name]
        try:
            DocumentType(doc_type_name)
        except ValueError as exc:
            raise RegistryError(
                f"{rules_path}: tag {tag_name!r} references unknown document type "
                f"{doc_type_name!r}"
            ) from exc
        return

    if key_name == "numeric_diff":
        spec = tree["numeric_diff"]
        fields = spec.get("fields", [])
        if len(fields) != 2:
            raise RegistryError(
                f"{rules_path}: tag {tag_name!r} numeric_diff needs exactly two fields"
            )
        for field_name in fields:
            if field_name not in FIELD_NAMES:
                raise RegistryError(
                    f"{rules_path}: tag {tag_name!r} numeric_diff references unknown "
                    f"field {field_name!r}"
                )
        if "min" not in spec:
            raise RegistryError(f"{rules_path}: tag {tag_name!r} numeric_diff missing min")
        try:
            float(spec["min"])
        except (TypeError, ValueError) as exc:
            raise RegistryError(
                f"{rules_path}: tag {tag_name!r} numeric_diff min must be numeric"
            ) from exc
        return

    if key_name == "requires_tag":
        required = tree["requires_tag"]
        required_key = key(required)
        if required_key not in tag_order:
            raise RegistryError(
                f"{rules_path}: tag {tag_name!r} requires_tag {required!r} is not in the registry"
            )
        if tag_order[required_key] >= tag_order[key(tag_name)]:
            raise RegistryError(
                f"{rules_path}: tag {tag_name!r} requires_tag {required!r} must appear "
                "earlier in TAGS.docx row order"
            )
        return

    if key_name == "text":
        pattern = tree["text"]
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise RegistryError(
                f"{rules_path}: tag {tag_name!r} text has invalid regex {pattern!r}: {exc}"
            ) from exc
        return

    raise RegistryError(f"{rules_path}: tag {tag_name!r} has unknown condition {key_name!r}")


def _validate_predicates(registry: Registry, rules_path: Path) -> None:
    tag_order = {tag.key: index for index, tag in enumerate(registry.tags)}
    for tag in registry.rulable:
        assert tag.rule is not None
        _validate_predicate_tree(
            tag.rule.tree,
            rules_path=rules_path,
            tag_name=tag.name,
            tag_order=tag_order,
        )


def build_registry(docx_path: Path, rules_path: Path) -> Registry:
    entries = _parse_docx(docx_path)
    if len(entries) < 10:
        raise RegistryError(
            f"{docx_path}: parsed only {len(entries)} tag(s) -- expected 40+; "
            "the table layout may have changed"
        )
    defs = _merge(entries)
    rules = _load_rules(rules_path)

    known_keys = {d.key for d in defs}
    unknown = sorted(rule.rule_id for key, rule in rules.items() if key not in known_keys)
    if unknown:
        raise RegistryError(f"{rules_path} names tag(s) not present in {docx_path.name}: {unknown}")

    tags = tuple(
        d
        if d.key not in rules
        else TagDefinition(name=d.name, key=d.key, reason=d.reason, rule=rules[d.key])
        for d in defs
    )
    registry = Registry(tags=tags)
    _validate_predicates(registry, rules_path)
    return registry


@lru_cache(maxsize=1)
def load_registry() -> Registry:
    """Cached: one registry per process, same convention as
    `app.api.deps.get_ocr_engine`/`get_llm_provider`. Call `load_registry.cache_clear()`
    in a test that needs to reload after editing the TOML/docx on disk."""
    from app.core.config import get_settings

    settings = get_settings()
    return build_registry(settings.tags_docx_path, settings.tag_rules_path)
