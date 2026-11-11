"""Runs classify()+extract() for every cached document (see build_cache.py)
against the real LLM, then either writes draft answer keys or scores the
result against hand-corrected ones.

    docker compose exec worker python -m scripts.prompt_eval.run --bootstrap
    # hand-correct sample/eval/answer_key/*/*.json against the SOURCE DOCS
    docker compose exec worker python -m scripts.prompt_eval.run
"""

import argparse
import json
import time
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.llm.base import LlmProvider
from app.schemas.extraction import PatientExtraction
from app.services import classification
from app.services.extraction_service import ExtractionService
from scripts.prompt_eval import score as scoring
from scripts.prompt_eval._common import ANSWER_KEY_DIR, OCR_CACHE_DIR, RAW_RESPONSES_DIR


@dataclass
class _TeeLlmProvider:
    """Wraps the real provider and keeps every raw completion from the most
    recent extract() call. Diagnostic only, never scored: lets a flat score
    be told apart as a prompt failure vs. the hallucination guard nulling a
    genuinely correct value (extraction_service.py's grounding step)."""

    inner: LlmProvider
    responses: list[str] = field(default_factory=list)

    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        text = self.inner.complete(system=system, user=user, temperature=temperature)
        self.responses.append(text)
        return text

    @property
    def version(self) -> str:
        return self.inner.version


def _build_extraction_service() -> tuple[ExtractionService, _TeeLlmProvider]:
    from app.api.deps import get_llm_provider

    settings = get_settings()
    tee = _TeeLlmProvider(inner=get_llm_provider())
    service = ExtractionService(
        tee,
        temperature=settings.llm_temperature,
        cbo_address=settings.sop_cbo_address,
        alias_name=settings.sop_alias_name,
    )
    return service, tee


def main(*, bootstrap: bool) -> None:
    service, tee = _build_extraction_service()
    cache_paths = sorted(OCR_CACHE_DIR.glob("*/*.json"))
    if not cache_paths:
        print(f"no cached docs under {OCR_CACHE_DIR} -- run build_cache first")
        return

    scored: list[tuple[str, str, PatientExtraction]] = []
    for cache_path in cache_paths:
        encounter = cache_path.parent.name
        doc_key = cache_path.stem
        cached = json.loads(cache_path.read_text())

        doc_type = classification.classify(cached["raw_text"])
        tee.responses.clear()
        started = time.perf_counter()
        outcome = service.extract(doc_type, cached["raw_text"], cached["page_texts"])
        duration = time.perf_counter() - started

        raw_path = RAW_RESPONSES_DIR / encounter / f"{doc_key}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps({"responses": tee.responses, "repaired": outcome.repaired}))

        print(
            f"{encounter}/{doc_key}: {doc_type.value} "
            f"({duration:.1f}s, repaired={outcome.repaired})"
        )

        if bootstrap:
            key_path = ANSWER_KEY_DIR / encounter / f"{doc_key}.json"
            if not key_path.exists():
                key_path.parent.mkdir(parents=True, exist_ok=True)
                key_path.write_text(outcome.fields.model_dump_json(indent=2))
                print(f"  draft key written -- correct it against the source document: {key_path}")
        else:
            scored.append((encounter, doc_key, outcome.fields))

    if bootstrap:
        print(
            "\nBootstrap complete. Hand-correct each file in sample/eval/answer_key/ against "
            'the SOURCE DOCUMENT, not this draft -- see README.md. Only the "value" fields '
            "are scored."
        )
        return

    scoring.report(scored)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap", action="store_true", help="write draft answer keys instead of scoring"
    )
    args = parser.parse_args()
    main(bootstrap=args.bootstrap)
