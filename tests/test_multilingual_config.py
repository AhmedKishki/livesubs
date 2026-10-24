import pytest

from livesubs.config import SessionSettings, Settings
from livesubs.languages import mode_for


@pytest.mark.parametrize('speaker', ['auto', 'en', 'de'])
def test_fixed_offer(speaker):
    assert mode_for(speaker).targets == ('en', 'de', 'ar')


def test_defaults_and_independent_fallback():
    assert SessionSettings().speaker == 'auto'
    assert Settings(session={'default_language': 'ar'}).default_language == 'ar'


def test_legacy_speaker_warns_and_normalises():
    with pytest.warns(UserWarning, match='session.speaker.*auto'):
        assert SessionSettings(speaker='en+de').speaker == 'auto'


@pytest.mark.parametrize('targets', [['de'], ['en', 'de', 'ar', 'ar'], ['en', 'de', 'xx']])
def test_target_narrowing_rejected(targets):
    with pytest.raises(ValueError, match='remove session.targets'):
        SessionSettings(targets=targets)


def test_full_legacy_offer_normalised():
    with pytest.warns(UserWarning, match='session.targets'):
        session = SessionSettings(targets=['ar', 'de', 'en'])
    assert session.targets == ['en', 'de', 'ar']


def test_fallback_environment(monkeypatch):
    monkeypatch.setenv('LIVESUBS_SESSION_DEFAULT_LANGUAGE', 'de')
    assert Settings().default_language == 'de'
