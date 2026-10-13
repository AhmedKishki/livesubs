"""Tests for the API key store.

The key paths matter operationally: a half-written env file or a silently wrong
precedence order would produce a confusing failure immediately before a lecture.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from livesubs.config import Settings, env_files
from livesubs.keystore import (
    PROVIDERS,
    active_providers,
    global_env_path,
    insecure_files,
    key_statuses,
    mask,
    planned_providers,
    project_env_path,
    read_env_file,
    remove_env_var,
    resolve_key,
    upsert_env_var,
)

# --- masking -----------------------------------------------------------------


def test_mask_never_reveals_more_than_the_last_four() -> None:
    rendered = mask("sk-1234567890abcdef")
    assert "1234567890" not in rendered
    assert rendered.endswith("(19 chars)")
    assert "cdef" in rendered


def test_mask_hides_very_short_values_entirely() -> None:
    assert mask("ab") == "**"
    assert mask("") == "(empty)"


# --- parsing -----------------------------------------------------------------


def test_read_env_file_parses_and_ignores_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# a comment\n"
        "\n"
        'QUOTED="value with spaces"\n'
        "SINGLE='single'\n"
        "export EXPORTED=yes\n"
        "BARE=plain\n"
        "INLINE=trimmed  # trailing\n",
        encoding="utf-8",
    )

    values = read_env_file(path)
    assert values["QUOTED"] == "value with spaces"
    assert values["SINGLE"] == "single"
    assert values["EXPORTED"] == "yes"
    assert values["BARE"] == "plain"
    assert values["INLINE"] == "trimmed"


def test_read_env_file_on_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_env_file(tmp_path / "nope") == {}


# --- writing -----------------------------------------------------------------


def test_upsert_appends_when_absent(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# existing\nFOO=bar\n", encoding="utf-8")

    upsert_env_var(path, "DEEPGRAM_API_KEY", "abc123")

    text = path.read_text(encoding="utf-8")
    assert "# existing" in text                    # comment preserved
    assert "FOO=bar" in text                       # unrelated var preserved
    assert "DEEPGRAM_API_KEY=abc123" in text


def test_upsert_replaces_in_place_without_duplicating(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\nDEEPSEEK_API_KEY=old\nB=2\n", encoding="utf-8")

    upsert_env_var(path, "DEEPSEEK_API_KEY", "new")

    assert path.read_text(encoding="utf-8").splitlines() == [
        "A=1",
        "DEEPSEEK_API_KEY=new",
        "B=2",
    ]


def test_upsert_reuses_a_commented_placeholder(tmp_path: Path) -> None:
    """A file seeded from .env.example should stay tidy, not gain duplicates."""

    path = tmp_path / ".env"
    path.write_text("# DEEPGRAM_API_KEY=\nOTHER=x\n", encoding="utf-8")

    upsert_env_var(path, "DEEPGRAM_API_KEY", "secret")

    text = path.read_text(encoding="utf-8")
    assert text.count("DEEPGRAM_API_KEY") == 1
    assert "DEEPGRAM_API_KEY=secret" in text
    assert "# DEEPGRAM_API_KEY=" not in text


def test_upsert_writes_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    upsert_env_var(path, "KEY", "value")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_upsert_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / ".env"
    upsert_env_var(path, "KEY", "value")
    assert read_env_file(path)["KEY"] == "value"


def test_remove_env_var(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\nOPENAI_API_KEY=x\nB=2\n", encoding="utf-8")

    assert remove_env_var(path, "OPENAI_API_KEY") is True
    assert read_env_file(path) == {"A": "1", "B": "2"}

    assert remove_env_var(path, "OPENAI_API_KEY") is False


# --- precedence --------------------------------------------------------------


def test_environment_beats_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_text("DEEPGRAM_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "from-environment")

    value, source = resolve_key(PROVIDERS["deepgram"], path)
    assert value == "from-environment"
    assert source == "environment"


def test_project_env_beats_the_global_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    global_path = global_env_path()
    global_path.parent.mkdir(parents=True)
    global_path.write_text("DEEPSEEK_API_KEY=global-value\n", encoding="utf-8")

    project = tmp_path / ".env"
    project.write_text("DEEPSEEK_API_KEY=project-value\n", encoding="utf-8")

    value, source = resolve_key(PROVIDERS["deepseek"], project)
    assert value == "project-value"
    assert source == ".env"


def test_global_env_is_used_as_a_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    global_path = global_env_path()
    global_path.parent.mkdir(parents=True)
    global_path.write_text("OPENAI_API_KEY=global-only\n", encoding="utf-8")

    value, source = resolve_key(PROVIDERS["openai"], tmp_path / "absent.env")
    assert value == "global-only"
    assert source.startswith("~/.config")


def test_unset_provider_reports_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    value, source = resolve_key(PROVIDERS["openai"], tmp_path / "absent.env")
    assert value is None
    assert source == "unset"


def test_settings_precedence_matches_the_documented_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``config.env_files`` claims project overrides global.

    Pinned here so that a pydantic-settings behaviour change cannot silently
    invert the precedence of a saved credential.
    """

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (project_dir / ".env").write_text("DEEPGRAM_API_KEY=project-wins\n", encoding="utf-8")

    global_path = global_env_path()
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text("DEEPGRAM_API_KEY=global-loses\n", encoding="utf-8")

    monkeypatch.chdir(project_dir)
    settings = Settings(_env_file=env_files())

    key = settings.provider_key("deepgram")
    assert key is not None
    assert key.get_secret_value() == "project-wins"


def test_environment_still_overrides_a_saved_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is what makes ``export KEY=...`` a session-only override."""

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "session-only")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (project_dir / ".env").write_text("DEEPGRAM_API_KEY=saved\n", encoding="utf-8")

    monkeypatch.chdir(project_dir)
    settings = Settings(_env_file=env_files())

    key = settings.provider_key("deepgram")
    assert key is not None
    assert key.get_secret_value() == "session-only"


# --- discovery and reporting -------------------------------------------------


def test_project_env_path_walks_up_to_this_projects_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "livesubs"\n', encoding="utf-8")
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)

    assert project_env_path(nested) == tmp_path / ".env"


def test_another_projects_pyproject_does_not_capture_the_env_file(tmp_path: Path) -> None:
    """Run from inside some other Python project, ``--set-key`` wrote into its ``.env``."""

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n', encoding="utf-8")
    nested = tmp_path / "src"
    nested.mkdir()

    assert project_env_path(nested) == nested.resolve() / ".env"


def test_an_unreadable_pyproject_is_not_a_match(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("not [valid toml", encoding="utf-8")

    assert project_env_path(tmp_path) == tmp_path.resolve() / ".env"


def test_a_credential_file_others_can_read_is_reported(tmp_path: Path) -> None:
    loose, tight, absent = tmp_path / "loose.env", tmp_path / "tight.env", tmp_path / "none.env"
    loose.write_text("K=v\n")
    tight.write_text("K=v\n")
    loose.chmod(0o664)
    tight.chmod(0o600)

    assert insecure_files([loose, tight, absent]) == [loose]


def test_files_the_keystore_writes_are_never_reported(tmp_path: Path) -> None:
    path = upsert_env_var(tmp_path / ".env", "DEEPGRAM_API_KEY", "secret-value")

    assert insecure_files([path]) == []


def test_key_statuses_covers_every_registered_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for provider in PROVIDERS.values():
        monkeypatch.delenv(provider.env_var, raising=False)

    statuses = key_statuses(tmp_path / "absent.env")

    assert [status.provider.name for status in statuses] == list(PROVIDERS)
    assert all(not status.set for status in statuses)


def test_render_does_not_leak_the_secret(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("DEEPGRAM_API_KEY=super-secret-token-abcd\n", encoding="utf-8")

    rendered = key_statuses(path)[0].render()

    assert "super-secret-token" not in rendered
    assert "abcd" in rendered


# --- active vs planned providers ---------------------------------------------


def test_deepl_remains_available_as_a_planned_provider() -> None:
    """DeepL is not rejected: it is registered for later integration.

    It must be marked planned so that neither ``--keys`` nor startup validation
    can treat it as working before an adapter exists.
    """

    assert "deepl" in PROVIDERS
    provider = PROVIDERS["deepl"]
    assert provider.planned
    assert provider.status == "planned"
    assert provider.role == "mt"


def test_active_providers_exclude_planned_ones() -> None:
    assert {provider.name for provider in active_providers()} == {"deepgram", "deepseek"}
    assert [provider.name for provider in active_providers("mt")] == ["deepseek"]
    assert [provider.name for provider in active_providers("asr")] == ["deepgram"]


def test_planned_providers_are_listed_separately() -> None:
    assert [provider.name for provider in planned_providers()] == ["openai", "deepl"]
    assert [provider.name for provider in planned_providers("mt")] == ["deepl"]
    assert [provider.name for provider in planned_providers("asr")] == ["openai"]


def test_planned_provider_render_is_visibly_marked(tmp_path: Path) -> None:
    """A stored key for a planned provider must not look like a working one."""

    path = tmp_path / ".env"
    path.write_text("DEEPL_API_KEY=super-secret-abcd\n", encoding="utf-8")

    rendered = next(
        status for status in key_statuses(path) if status.provider.name == "deepl"
    ).render()

    assert "planned" in rendered
    assert "abcd" in rendered                          # masked, last four only
    assert "super-secret" not in rendered


def test_deepl_key_alone_does_not_satisfy_mt_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the bug that the planned/active split fixed.

    ``validate_for_startup`` previously accepted either DEEPSEEK_API_KEY or
    DEEPL_API_KEY, but no DeepL adapter was ever written. A DEEPL_API_KEY on its
    own would therefore have passed validation while the service ran with no
    working translation stage.
    """

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("DEEPL_API_KEY", "dl-test")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # Away from the repository, because a developer keeps their own .env there - the
    # gitignored file with their real keys in it - and reading it would answer this
    # question with somebody's credentials rather than with the ones set here.
    monkeypatch.chdir(tmp_path)

    problems = Settings(_env_file=env_files()).validate_for_startup()

    # The message is generated from the registry, so it names the active MT
    # provider rather than hardcoding one.
    assert any("deepseek" in problem.lower() for problem in problems), problems
