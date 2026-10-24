import pytest

from livesubs.speakers import (
    DEFAULT_SPEAKER_OPTIONS,
    MULTILINGUAL_SOURCE,
    normalize_speaker,
    routing_language,
)


def test_defaults():
    assert tuple(option.code for option in DEFAULT_SPEAKER_OPTIONS) == ('auto', 'en', 'de', 'ar')
    assert MULTILINGUAL_SOURCE == 'multilingual'


@pytest.mark.parametrize(('raw', 'expected'), [('ZH-hk', 'zh-HK'), ('zh-hant', 'zh-Hant'),
                                              ('en+de', 'auto'), (' AR-eg ', 'ar-EG')])
def test_normalize(raw, expected):
    assert normalize_speaker(raw) == expected


@pytest.mark.parametrize('raw', ['', 'en/de', 'multi', 'english', 'en_XX', 'en+ar'])
def test_invalid(raw):
    with pytest.raises(ValueError):
        normalize_speaker(raw)


def test_routing_preserves_non_target_distinctions():
    assert routing_language('en-GB') == 'en'
    assert routing_language('ar-EG') == 'ar'
    assert routing_language('de-CH') == 'de'
    assert routing_language('zh-HK') == 'zh-hk'
    assert routing_language('zh-Hant') == 'zh-hant'


def test_generic_config_mode_and_labels():
    from livesubs.config import Settings
    from livesubs.languages import LANGUAGES, mode_for

    settings = Settings(session={'speaker': 'ZH-hk'})
    assert settings.session.speaker == 'zh-HK'
    assert settings.source_language == 'zh-HK'
    assert mode_for('ar-EG').source == 'ar-EG'
    assert 'zh-HK' in settings.mode.label
    assert tuple(LANGUAGES) == ('en', 'de', 'ar')


def test_model_choices_before_credentials():
    from livesubs.approaches import speaker_options
    from livesubs.config import Settings

    settings = Settings(providers={'deepgram': {'options': {'model': 'nova-2'}}})
    codes = {option.code for option in speaker_options(settings)}
    assert 'zh-HK' in codes
    assert 'ar' not in codes
    assert 'auto' not in codes


def test_approach_metadata_and_immutable_manual_asr_config():
    from livesubs.approaches import pipeline_config, speaker_options
    from livesubs.config import Settings

    settings = Settings(session={'speaker': 'ar-EG'}, providers={
        'deepgram': {'options': {'model': 'nova-3-general', 'language': 'ar-EG'}}})
    assert {o.code for o in speaker_options(settings)} == {'ar', 'ar-EG'}
    assert pipeline_config(settings).asr.language == 'ar-EG'
    assert pipeline_config(Settings()).asr.language == 'en+de'
    for name in ('sim', 'live-sim'):
        assert speaker_options(settings, name) == DEFAULT_SPEAKER_OPTIONS
    assert 'zh-HK' in {o.code for o in speaker_options(settings, 'live-text')}


def test_cli_generic_syntax_and_legacy_warning():
    from livesubs.__main__ import _build_parser
    from livesubs.config import Settings

    parser = _build_parser()
    assert parser.parse_args(['--speaker', 'zh-HK']).speaker == 'zh-HK'
    with pytest.raises(SystemExit):
        parser.parse_args(['--speaker', 'not/a/code'])
    args = parser.parse_args(['--speaker', 'en+de'])
    with pytest.warns(UserWarning, match='deprecated'):
        assert Settings().for_session(speaker=args.speaker).session.speaker == 'auto'


def test_case_insensitive_legacy_alias_still_warns():
    from livesubs.config import Settings
    from livesubs.languages import mode_for

    with pytest.warns(UserWarning, match='deprecated'):
        assert Settings(session={'speaker': ' EN+DE '}).session.speaker == 'auto'
    with pytest.warns(UserWarning, match='deprecated'):
        assert mode_for('EN+DE').source == 'en+de'


@pytest.mark.parametrize('action', ['--check', '--preflight'])
def test_cli_rejects_model_incompatible_pin_before_runtime(monkeypatch, action):
    from livesubs import __main__ as cli
    from livesubs import preflight
    from livesubs.config import Settings

    settings = Settings(providers={'deepgram': {'options': {'model': 'nova-2'}}})
    monkeypatch.setattr(cli, 'get_settings', lambda path: settings)
    monkeypatch.setattr(preflight, '_main', lambda *a, **kw: pytest.fail('preflight reached'))
    assert cli.main([action, '--speaker', 'ar']) == 2
