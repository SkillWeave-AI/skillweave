"""A council result can be copied, pickled and serialised without losing who answered.

``CouncilEngine.deliberate()`` keeps the adapter's ``AttributedResponse`` in
``ModelResponse.response``, ``Ranking.raw_text`` and ``SynthesisResult.content``.
``AttributedResponse`` is a ``str`` whose ``__new__`` takes the attribution as
keyword-only arguments. ``copy`` and ``pickle`` rebuild a ``str`` subclass by
calling ``__new__`` with the text alone, so ``dataclasses.asdict()`` and
``copy.deepcopy()`` on a finished result raised::

    TypeError: AttributedResponse.__new__() missing 2 required keyword-only
    arguments: 'requested_model' and 'answering_model'

A consumer that serialised the run record after a full, paid deliberation lost
every answer. A copy that survives but drops the attribution would be the same
defect in a quieter form, so every test also checks who answered.
"""

import asyncio
import copy
import dataclasses
import json
import pickle

import pytest

from skillweave.council.engine import (
    CouncilConfig,
    CouncilEngine,
    CouncilResult,
    ModelResponse,
    Ranking,
    SynthesisResult,
)
from skillweave.council.synthesis import validate_output
from skillweave.routing.faigate_adapter import AttributedResponse

_ATTRIBUTION = (
    "requested_model",
    "answering_model",
    "served_by",
    "provider",
    "is_substituted",
    "substituted",
)


def _attributed(content, requested, answering, *, served_by=None):
    served = served_by or answering
    return AttributedResponse(
        content,
        requested_model=requested,
        answering_model=answering,
        provider="faigate",
        served_by=served,
        is_substituted=served != requested,
    )


def _assert_same_attribution(copied, original):
    assert type(copied) is AttributedResponse
    assert copied == original
    for name in _ATTRIBUTION:
        assert getattr(copied, name) == getattr(original, name), name


def _finished_result():
    """A council result holding ``AttributedResponse`` where ``deliberate()`` puts it."""
    answer = _attributed("opinion of sonnet", "sonnet", "deepseek-v4-flash")
    review = _attributed("FINAL RANKING:\n1. Response A", "deepseek-v4-pro", "deepseek-v4-pro")
    # The envelope names the chairman; the gateway header names the model that served.
    synthesis = _attributed("final answer", "glm-5-2", "glm-5-2", served_by="deepseek-v4-flash")
    return CouncilResult(
        query="Which transport?",
        stage1=[
            ModelResponse(
                model_id="sonnet",
                response=answer,
                elapsed_ms=1.0,
                answering_model=answer.answering_model,
                requested_model="sonnet",
                status="substituted",
                provider="faigate",
                served_by=answer.served_by,
            )
        ],
        stage2=[
            Ranking(
                reviewer="deepseek-v4-pro",
                rankings={"A": 1},
                raw_text=review,
                reviewer_answering_model=review.answering_model,
                provider="faigate",
            )
        ],
        stage3=SynthesisResult(
            chairman_model="glm-5-2",
            content=synthesis,
            format="markdown",
            elapsed_ms=2.0,
            chairman_answering_model=synthesis.answering_model,
            provider="faigate",
        ),
    )


def test_asdict_keeps_every_answer_and_who_answered():
    """RED against 514ed0b: ``TypeError`` from ``AttributedResponse.__new__()``."""
    result = _finished_result()

    record = dataclasses.asdict(result)

    _assert_same_attribution(record["stage1"][0]["response"], result.stage1[0].response)
    _assert_same_attribution(record["stage2"][0]["raw_text"], result.stage2[0].raw_text)
    _assert_same_attribution(record["stage3"]["content"], result.stage3.content)


def test_deepcopy_keeps_every_answer_and_who_answered():
    """RED against 514ed0b: ``TypeError`` from ``AttributedResponse.__new__()``."""
    result = _finished_result()

    copied = copy.deepcopy(result)

    _assert_same_attribution(copied.stage1[0].response, result.stage1[0].response)
    _assert_same_attribution(copied.stage2[0].raw_text, result.stage2[0].raw_text)
    _assert_same_attribution(copied.stage3.content, result.stage3.content)


def _pickle_roundtrip(protocol):
    return lambda value: pickle.loads(pickle.dumps(value, protocol=protocol))


@pytest.mark.parametrize(
    "roundtrip",
    [
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
        *(
            pytest.param(_pickle_roundtrip(protocol), id=f"pickle-{protocol}")
            for protocol in range(pickle.HIGHEST_PROTOCOL + 1)
        ),
    ],
)
def test_attributed_response_roundtrip_keeps_attribution(roundtrip):
    """Pickle protocols 0 and 1 rebuild through ``str.__new__`` and always worked.

    Every other path calls ``AttributedResponse.__new__()`` and was RED against
    514ed0b.
    """
    original = _attributed("final answer", "glm-5-2", "glm-5-2", served_by="deepseek-v4-flash")

    _assert_same_attribution(roundtrip(original), original)


class _FaigateLikeProvider:
    """Replies the way the Faigate adapter does: every reply is an ``AttributedResponse``."""

    SYNTHESIS = {
        "title": "Transport decision",
        "summary": "The council prefers calling the router over HTTP to embedding a copy of it.",
        "key_insights": ["An embedded router copy drifts from the router it was copied from"],
        "consensus_score": 0.8,
        "dissent": None,
        "sources": [],
    }

    def __init__(self, answering):
        self.answering = dict(answering)  # requested -> answering model

    async def query(self, model, messages, temperature=0.5):
        prompt = messages[-1]["content"]
        if "Rank these responses" in prompt:
            content = "FINAL RANKING:\n1. Response A — grounded\n2. Response B — thinner"
        elif "Chairman" in prompt:
            content = json.dumps(self.SYNTHESIS)
        else:
            content = f"opinion of {model}"
        return _attributed(content, model, self.answering.get(model, model))


def test_deliberated_json_result_serialises_with_attribution():
    """The consumer path: a full JSON-mode deliberation, then ``asdict()`` and ``json.dumps()``.

    RED against 514ed0b: ``TypeError`` from ``AttributedResponse.__new__()``. The
    council's own JSON path is not where it broke: ``validate_output()`` strips
    the chairman's text to a plain ``str`` before ``json.loads()`` and never
    copies the result.
    """
    provider = _FaigateLikeProvider({"sonnet": "deepseek-v4-flash"})
    config = CouncilConfig(
        models=["sonnet", "deepseek-v4-pro"],
        chairman="glm-5-2",
        mode="full",
        output_format="json",
    )
    result = asyncio.run(CouncilEngine(provider).deliberate("Which transport?", config))

    record = json.loads(json.dumps(dataclasses.asdict(result)))

    assert [seat["response"] for seat in record["stage1"]] == [
        "opinion of sonnet",
        "opinion of deepseek-v4-pro",
    ]
    assert [seat["answering_model"] for seat in record["stage1"]] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ]
    assert len(record["stage2"]) == 2
    ok, data, err = validate_output(record["stage3"]["content"])
    assert ok, err
    assert data == _FaigateLikeProvider.SYNTHESIS

    copied = copy.deepcopy(result)
    _assert_same_attribution(copied.stage1[0].response, result.stage1[0].response)
    _assert_same_attribution(copied.stage2[0].raw_text, result.stage2[0].raw_text)
    _assert_same_attribution(copied.stage3.content, result.stage3.content)
