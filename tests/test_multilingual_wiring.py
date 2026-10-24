import pytest

from livesubs import approaches
from livesubs.config import Settings


@pytest.mark.parametrize('first_now', [False, True])
def test_translators_are_all_lazy(monkeypatch, first_now):
    calls = []
    monkeypatch.setattr(approaches, 'validate_mt', lambda settings: None)
    monkeypatch.setattr(approaches, 'build_mt', lambda settings, target: calls.append(target))
    built = approaches.translators(Settings(), first_now=first_now)
    assert tuple(built) == ('en', 'de', 'ar')
    assert calls == []
    built['de']()
    assert calls == ['de']


def test_live_sim_wiring(monkeypatch):
    import livesubs.pipeline

    monkeypatch.setattr(livesubs.pipeline, 'LivePipeline', lambda **kwargs: kwargs)
    monkeypatch.setattr(approaches, 'artefacts', lambda settings, capture: (capture, None))
    result = approaches.build_approach('live-sim', Settings(session={'speaker': 'de'}))
    assert result['source_language'] == 'en+de'
    assert result['speaker'] == 'de'
    assert tuple(result['translators']) == ('en', 'de', 'ar')
    assert result['config'].asr.language == 'de'


@pytest.mark.parametrize('speaker', ['auto', 'en', 'de'])
def test_mt_source_is_stable(monkeypatch, speaker):
    import livesubs.mt.openai_compat

    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-only-placeholder')
    monkeypatch.setattr(livesubs.mt.openai_compat, 'OpenAiCompatMt', lambda **kwargs: kwargs)
    result = approaches.build_mt(Settings(session={'speaker': speaker}), 'en')
    assert result['source_language'] == 'en+de'
    assert result['target_language'] == 'en'


def test_lazy_translators_reject_unavailable_adapter():
    settings = Settings(
        providers={'unsupported': {'role': 'mt', 'requires_key': False}},
        pipeline={'mt': ['unsupported']},
    )
    with pytest.raises(NotImplementedError, match='adapter'):
        approaches.translators(settings)


def test_build_asr_rejects_override_before_demand(monkeypatch):
    monkeypatch.setenv('DEEPGRAM_API_KEY', 'test-only-placeholder')
    settings = Settings(providers={'deepgram': {'options': {'language': 'en'}}})
    with pytest.raises(ValueError, match='contradicts'):
        approaches.build_asr(settings)
