import os

from livesubs.audio import selection


def test_long_catalogue_renders_bounded_window_with_selected_entry(monkeypatch):
    output = []
    monkeypatch.setattr(os, 'get_terminal_size', lambda fd: os.terminal_size((80, 10)))
    monkeypatch.setattr(os, 'write', lambda fd, data: output.append(data))
    entries = [(str(i), f'Language {i}') for i in range(120)]
    lines = selection._render(1, entries, 110, 0)
    assert lines <= 8
    rendered = b''.join(output)
    assert b'> Language 110' in rendered
    assert b'Language 0 ' not in rendered
    assert b'120' in rendered
