from urllib.parse import parse_qs, urlparse

import pytest

from livesubs.asr.base import AsrConfig
from livesubs.asr.deepgram import DeepgramAsr
from livesubs.asr.deepgram_languages import speaker_options, validate_selection


@pytest.mark.parametrize('model', ['nova-3', 'nova-3-general', 'nova-2', 'nova-2-general',
                                  'nova-3-medical', 'nova-3-pharma', 'nova-2-phonecall',
                                  'nova-2-meeting', 'nova-2-finance', 'nova-2-conversationalai',
                                  'nova-2-voicemail', 'nova-2-video', 'nova-2-medical',
                                  'nova-2-drivethru', 'nova-2-automotive', 'nova-2-atc'])
def test_every_advertised_option_reaches_uri_exactly(model):
    for option in speaker_options(model):
        cfg = AsrConfig(language='en+de' if option.code == 'auto' else option.code)
        uri = DeepgramAsr(api_key='test', model=model)._uri(cfg)
        assert parse_qs(urlparse(uri).query)['language'] == [
            'multi' if option.code == 'auto' else option.code]


@pytest.mark.parametrize('model', ['flux-general-en', 'nova-3-custom', 'nova-2-custom', 'bogus'])
def test_unknown_models_rejected(model):
    with pytest.raises(ValueError, match='model'):
        speaker_options(model)


def test_model_coverage_and_override_pins():
    assert validate_selection('nova-3-general', 'ZH-hk') == 'zh-HK'
    assert validate_selection('nova-3', 'en', 'en-GB') == 'en-GB'
    with pytest.raises(ValueError):
        validate_selection('nova-3', 'en-US', 'en-GB')
    for model, code in [('nova-2', 'ar'), ('nova-3', 'xh'), ('nova-3', 'en-CA'),
                        ('nova-2', 'tr-TR'), ('nova-3-medical', 'ar'), ('nova-2', 'auto')]:
        with pytest.raises(ValueError):
            validate_selection(model, code)
    assert {o.code for o in speaker_options('nova-3', 'ar-EG')} == {'ar', 'ar-EG'}
    assert tuple(o.code for o in speaker_options('nova-2-phonecall')) == ('en', 'en-US')


def test_exact_documented_general_code_sets():
    # Independent transcription of the official monolingual rows, checked 2026-10-24.
    nova3 = {
        'af', 'af-ZA', 'ar', 'ar-AE', 'ar-SA', 'ar-QA', 'ar-KW', 'ar-SY', 'ar-LB',
        'ar-PS', 'ar-JO', 'ar-EG', 'ar-SD', 'ar-TD', 'ar-MA', 'ar-DZ', 'ar-TN', 'ar-IQ',
        'ar-IR', 'hy', 'as', 'as-IN', 'be', 'bn', 'bs', 'bg', 'ca', 'zh-HK', 'zh', 'zh-CN',
        'zh-Hans', 'zh-TW', 'zh-Hant', 'hr', 'cs', 'cs-CZ', 'da', 'da-DK', 'nl', 'en',
        'en-US', 'en-AU', 'en-GB', 'en-IN', 'en-NZ', 'et', 'fi', 'nl-BE', 'fr', 'fr-CA',
        'ka', 'ka-GE', 'de', 'de-CH', 'el', 'gu', 'gu-IN', 'he', 'hi', 'hu', 'id', 'it',
        'ja', 'kn', 'kk', 'kk-KZ', 'ko', 'ko-KR', 'lv', 'lt', 'mk', 'ms', 'mr', 'mn',
        'ne', 'no', 'ps', 'ps-AF', 'fa', 'pl', 'pt', 'pt-BR', 'pt-PT', 'pa', 'pa-IN',
        'ro', 'ru', 'sr', 'sk', 'sl', 'es', 'es-419', 'sv', 'sv-SE', 'tl', 'ta', 'te',
        'th', 'th-TH', 'tr', 'tr-TR', 'uk', 'ur', 'vi',
    }
    nova2 = {
        'bg', 'ca', 'zh', 'zh-CN', 'zh-Hans', 'zh-TW', 'zh-Hant', 'zh-HK', 'cs', 'da',
        'da-DK', 'nl', 'en', 'en-US', 'en-AU', 'en-GB', 'en-NZ', 'en-IN', 'et', 'fi',
        'nl-BE', 'fr', 'fr-CA', 'de', 'de-CH', 'el', 'hi', 'hu', 'id', 'it', 'ja', 'ko',
        'ko-KR', 'lv', 'lt', 'ms', 'no', 'pl', 'pt', 'pt-BR', 'pt-PT', 'ro', 'ru',
        'sk', 'es', 'es-419', 'sv', 'sv-SE', 'th', 'th-TH', 'tr', 'uk', 'vi',
    }
    assert {o.code for o in speaker_options('nova-3')} == nova3 | {'auto'}
    assert {o.code for o in speaker_options('nova-2')} == nova2
    assert speaker_options('nova-3-general') == speaker_options('nova-3')
    assert speaker_options('nova-2-general') == speaker_options('nova-2')


def test_specialist_sets_and_script_override_distinction():
    expected = {'en', 'en-US', 'en-AU', 'en-CA', 'en-GB', 'en-IE', 'en-IN', 'en-NZ'}
    for model in ('nova-3-medical', 'nova-3-pharma'):
        assert {o.code for o in speaker_options(model)} == expected
    assert {o.code for o in speaker_options('nova-3', 'multi')} == {'auto'}
    for override in ('zh-HK', 'zh-TW', 'zh-Hant'):
        with pytest.raises(ValueError):
            validate_selection('nova-3', 'zh', override)
    assert validate_selection('nova-3', 'zh', 'zh-Hans') == 'zh-Hans'


def test_canonical_locale_reaches_uri_and_contradictions_fail_before_network():
    assert 'language=zh-HK' in DeepgramAsr(api_key='test')._uri(AsrConfig(language='ZH-hk'))
    adapter = DeepgramAsr(api_key='test', language='en-GB')
    assert 'language=en-GB' in adapter._uri(AsrConfig(language='en'))
    with pytest.raises(ValueError, match='contradicts'):
        adapter.validate_config(AsrConfig(language='en-US'))
