import os
import tempfile
from pathlib import Path

# Set before any app module imports Settings. The engine is constructed at import
# time in app.db.session, and it needs a URL even though no test connects to it.
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_APP_USER", "test_app")
os.environ.setdefault("POSTGRES_APP_PASSWORD", "test")
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("DATA_DIR", str(Path(tempfile.gettempdir()) / "local-doc-ocr-test"))
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-that-is-comfortably-over-32-chars")

# app.main's lifespan loads the tag registry (app/services/tag_registry.py) at
# startup and fails to start if it can't. In Docker the docx is mounted at
# /srv/app/TAGS.docx; on a host/CI run use files_for_project/TAGS.docx.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TAGS_DOCX_CONTAINER = Path("/srv/app/TAGS.docx")
_TAGS_DOCX_HOST = _REPO_ROOT / "files_for_project" / "TAGS.docx"
_TAGS_DOCX = _TAGS_DOCX_CONTAINER if _TAGS_DOCX_CONTAINER.is_file() else _TAGS_DOCX_HOST
os.environ.setdefault("TAGS_DOCX_PATH", str(_TAGS_DOCX))
os.environ.setdefault("TAG_RULES_PATH", str(_REPO_ROOT / "app" / "services" / "tag_rules.toml"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_llm_provider, get_ocr_engine  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.main import create_app  # noqa: E402
from app.worker import celery_app  # noqa: E402
from tests.fakes import FakeLlmProvider, FakeOcrEngine  # noqa: E402

# `.delay()` runs the task inline, in-process, with no broker -- same reason
# TestClient's synchronous BackgroundTasks execution used to make a real
# upload run the whole pipeline before the request returned.
celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    # One process-wide Limiter (app/core/rate_limit.py:limiter), so its
    # in-memory counters persist across tests unless cleared. Without this, a
    # test file with many logins/uploads trips the real limit and later tests
    # see 429s that have nothing to do with what they're testing.
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client(settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Override the OCR engine and LLM provider so upload tests never reach
    # paddle or LM Studio. `app.dependency_overrides` only affects FastAPI's
    # own DI -- `app.worker.process_document_task` (what `.delay()` now runs,
    # eagerly, see above) imported `get_ocr_engine`/`get_llm_provider` by
    # reference into its own module namespace, so that's what has to be
    # patched for a test upload to reach the fakes instead of the real
    # adapters.
    app = create_app(settings)
    app.dependency_overrides[get_ocr_engine] = lambda: FakeOcrEngine()
    app.dependency_overrides[get_llm_provider] = lambda: FakeLlmProvider()
    monkeypatch.setattr("app.worker.get_ocr_engine", lambda: FakeOcrEngine())
    monkeypatch.setattr("app.worker.get_llm_provider", lambda: FakeLlmProvider())
    return TestClient(app)
