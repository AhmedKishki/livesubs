from livesubs import launcher
from livesubs.config import Settings


def test_startup_choices_survive_reload(monkeypatch):
    monkeypatch.setattr(launcher, '_choose_target', lambda *args, **kwargs: 'mic')
    monkeypatch.setattr(launcher, '_candidate', lambda *args: None)
    monkeypatch.setattr(launcher, '_ensure_role', lambda *args, **kwargs: ('fake', True, None))
    monkeypatch.setattr(launcher, '_reload_settings', lambda path: Settings())
    monkeypatch.setattr(launcher, '_confirm_providers_unchanged', lambda *args: None)
    calls = []

    def select(title, entries, preferred):
        calls.append((title, preferred))
        return 'de' if len(calls) == 1 else 'ar'

    monkeypatch.setattr(launcher, '_select_setting', select, raising=False)
    result = launcher.prepare_live_start(Settings(), interactive=True)
    assert result.session.speaker == 'de'
    assert result.default_language == 'ar'
    assert calls[0][1] == 'auto'


def test_explicit_speaker_skips_speaker_picker(monkeypatch):
    monkeypatch.setattr(launcher, '_choose_target', lambda *args, **kwargs: 'mic')
    monkeypatch.setattr(launcher, '_candidate', lambda *args: None)
    monkeypatch.setattr(launcher, '_ensure_role', lambda *args, **kwargs: ('fake', False, None))
    calls = []
    monkeypatch.setattr(launcher, '_select_setting', lambda *args: calls.append(args) or 'ar', raising=False)
    result = launcher.prepare_live_start(
        Settings(session={'speaker': 'de'}), interactive=True, explicit_speaker=True
    )
    assert result.session.speaker == 'de'
    assert len(calls) == 1
