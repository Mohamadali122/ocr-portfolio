# Prompt eval harness

Scores the LLM extraction prompt (`app/services/extraction_service.py`) against a
hand-corrected answer key built from `sample/p1`..`p10` -- the same real documents
`scripts/eval_samples.py` already uses.

Distinct from that script: `eval_samples.py` drives the full HTTP pipeline and
reports *completeness* (did the document reach `completed`, how many fields got a
`typed_value`) with no answer key and no PHI in its output. This harness answers
the narrower question that one can't: is a given field's extracted *value*
actually correct? That needs an answer key, and needs handling the values
themselves -- see PHI below.

## Workflow

Everything runs inside a container built from the repo's `Dockerfile` (has
PaddleOCR, reaches LM Studio the same way `app.worker` does):

```powershell
# 1. Once: OCR every sample doc and cache raw_text/page_texts. ~23s/page --
#    do this once, not per prompt edit. Re-run with --force only if a source
#    doc under sample/ changes.
docker compose exec worker python -m scripts.prompt_eval.build_cache

# 2. Once: draft an answer key from the current pipeline's own output.
docker compose exec worker python -m scripts.prompt_eval.run --bootstrap

# 3. Hand-correct every file under sample/eval/answer_key/*/*.json --
#    against the SOURCE DOCUMENT, not the draft. Correcting from the model's
#    own output just re-approves its existing mistakes. Only the "value" key
#    of each field is scored; confidence/source_page/typed_value/source are
#    carried over for context and ignored.

# 4. Every time a prompt changes: re-score.
docker compose exec worker python -m scripts.prompt_eval.run
```

Step 4 only re-runs the LLM step (`ExtractionService.extract`), not OCR -- OCR
output doesn't change when a prompt does.

## What gets scored

The **final grounded value** (`ExtractedField.value`, after the hallucination
guard, before SOP normalization) -- what a reviewer actually sees. Not the raw
LLM response: a prompt can be right and still show as wrong here if OCR noise
kept the hallucination guard from grounding it. `run.py` dumps the raw LLM
response per doc to `sample/eval/raw_responses/` for exactly that diagnosis --
if a field reads `missed` and the raw response shows the model actually got it
right, the guard/OCR is the problem, not the prompt.

Match rule: exact after `extraction_service._normalize()` (whitespace-collapse
+ casefold). A unit or date-format difference counts as a miss on purpose --
the SOP normalizer is supposed to produce that canonical form downstream, not
the prompt.

Four buckets per field:
- `correct` -- matches (including both null)
- `wrong` -- key has a value, extraction has a different one
- `hallucinated` -- key is null, extraction has a value
- `missed` -- key has a value, extraction is null

`wrong` and `hallucinated` are the regression alarms; a `missed` field is
lower-stakes (a reviewer fills a blank; a wrong or invented value is what they
might not catch).

## Change discipline

Get a baseline scorecard before changing any prompt. After that, change only
the failure classes the scorecard actually shows -- no speculative edits.
Candidate changes held in reserve until the baseline says which matter:
insurance-slot ordering, stronger JSON-only steering / few-shot, narrative
bullet-count enforcement, raising the single-retry ceiling.

## PHI

Everything under `sample/eval/` is PHI, exactly like everything else under
`sample/` (gitignored wholesale -- see `.gitignore`). This harness's own code
(`scripts/prompt_eval/*.py`) is not PHI and is committed normally.

- Console output never prints a field value -- only field names, doc
  identifiers, and bucket counts, the same "names are safe, values are not"
  rule `extraction_service.py`'s hallucination guard already logs by.
- The full report (`sample/eval/reports/*.json`) *does* include actual values
  -- open it locally to review a `wrong`/`hallucinated` field, never paste its
  contents anywhere (a chat, an issue, Slack, a notes app).
- Never commit anything under `sample/`. It already isn't tracked; keep it
  that way.
