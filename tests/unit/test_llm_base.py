import pytest

from app.llm.base import LlmProvider
from tests.fakes import ExplodingLlmProvider, FakeLlmProvider


class TestProtocolConformance:
    @pytest.mark.parametrize("provider", [FakeLlmProvider(), ExplodingLlmProvider()])
    def test_fakes_satisfy_the_protocol(self, provider: object) -> None:
        assert isinstance(provider, LlmProvider)
