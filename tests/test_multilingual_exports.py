from types import SimpleNamespace

import pytest

from livesubs.asr.base import AsrConfig
from livesubs.asr.stub import StubAsr
from livesubs.asr.transcript import TranscriptAsr
from livesubs.events import HelloMessage, SegmentMessage, SegmentState
from livesubs.sim import SimPublisher, replay
from livesubs.subtitles import Cue, SubtitleRecorder


def test_source_is_independent_and_never_collides(tmp_path):
    recorder = SubtitleRecorder(directory=tmp_path, stem="test", source_code="en",
                                target_codes=("en", "de", "ar"))
    source = SimpleNamespace(seq=0, text="raw", start_ms=0, end_ms=1000)
    recorder.record_source(source)
    recorder.record_source(source)
    recorder(SimpleNamespace(seq=0, text="rendered", src="raw", language="en",
                             start_ms=0, end_ms=1000))
    recorder.close()
    assert (tmp_path / "test.source.srt").read_text().count("raw") == 1
    assert "raw" not in (tmp_path / "test.en.srt").read_text()


@pytest.mark.asyncio
async def test_auto_sim_routes_all_targets_without_transcription_drafts():
    messages = [message async for message in replay(speed=0)]
    hello = next(m for m in messages if isinstance(m, HelloMessage))
    assert hello.data.targets == ["en", "de", "ar"]
    segments = [m.data for m in messages if isinstance(m, SegmentMessage)]
    for seq, matching in enumerate(("en", "de", None, None)):
        rows = [s for s in segments if s.seq == seq]
        assert {s.language for s in rows} == {"en", "de", "ar"}
        if matching:
            own = [s for s in rows if s.language == matching]
            assert [(s.rev, s.state) for s in own] == [(0, SegmentState.FINAL)]
            assert own[0].text == own[0].src


def test_sim_speaker_controls():
    publisher = SimPublisher(speaker="auto")
    assert publisher.speaker_snapshot()["requested"] == "auto"
    assert publisher.set_speaker("de") is True
    assert publisher.set_speaker("de") is False
    assert publisher.speaker_snapshot()["revision"] == 1
    with pytest.raises(ValueError):
        publisher.set_speaker("ar")


@pytest.mark.asyncio
async def test_scripted_language_is_not_the_recognition_pin():
    stub = StubAsr()
    await stub.open(AsrConfig(language="de"))
    event = stub._make_event(kind="final", now_ms=1000, speech_final=True)
    assert event.language == "en"
    transcript = TranscriptAsr([Cue(0, 1000, "some text")])
    await transcript.open(AsrConfig(language="de"))
    await transcript.push(bytes(320))
    assert (await anext(transcript.events())).language is None


@pytest.mark.asyncio
async def test_transcript_accepts_trusted_source_language():
    transcript = TranscriptAsr([Cue(0, 1000, "Hallo")], source_language="de")
    await transcript.open(AsrConfig())
    await transcript.push(bytes(320))
    assert (await anext(transcript.events())).language == "de"


def test_source_order_does_not_follow_target_completion(tmp_path):
    recorder = SubtitleRecorder(directory=tmp_path, stem="ordered", source_code="en+de",
                                target_codes=("en", "de", "ar"))
    for seq in range(3):
        recorder.record_source(SimpleNamespace(seq=seq, text=f"raw {seq}",
                                              start_ms=seq * 1000, end_ms=(seq + 1) * 1000))
    for seq in (2, 0):
        recorder(SimpleNamespace(seq=seq, text=f"target {seq}", src="ignored",
                                 language="en", start_ms=seq * 1000, end_ms=(seq + 1) * 1000))
    recorder.close()
    raw = (tmp_path / "ordered.source.srt").read_text()
    assert raw.index("raw 0") < raw.index("raw 1") < raw.index("raw 2")
    assert "target 1" not in (tmp_path / "ordered.en.srt").read_text()


def test_raw_spans_and_writer_failures_are_isolated(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    recorder = SubtitleRecorder(directory=blocker, stem="bad", source_code="en",
                                target_codes=("en",))
    recorder.record_source(SimpleNamespace(seq=0, text="raw", start_ms=0, end_ms=1000))
    assert recorder.errors
    recorder.close()
    recorder = SubtitleRecorder(directory=tmp_path, stem="span", source_code="de",
                                target_codes=("en",))
    recorder.record_source(SimpleNamespace(seq=0, text="invalid", start_ms=1000, end_ms=0))
    recorder.record_source(SimpleNamespace(seq=1, text="<valid> & text", start_ms=1000, end_ms=2000))
    recorder.close()
    assert "invalid" not in (tmp_path / "span.source.srt").read_text()
    assert "&lt;valid&gt; &amp; text" in (tmp_path / "span.source.vtt").read_text()


@pytest.mark.asyncio
async def test_replay_runtime_changes_apply_only_to_next_commitment():
    publisher = SimPublisher()
    stream = replay(speed=0, speaker_getter=lambda: publisher.speaker,
                    on_source=publisher._source)
    finals = set()
    seen = {}
    async for message in stream:
        if not isinstance(message, SegmentMessage):
            continue
        segment = message.data
        key = (segment.seq, segment.language)
        assert key not in finals
        assert segment.rev > seen.get(key, -1)
        seen[key] = segment.rev
        if segment.state == SegmentState.FINAL:
            finals.add(key)
        if segment.seq == 0:
            publisher.set_speaker("de")
        if segment.seq == 1 and segment.language == "de":
            assert segment.text == segment.src
            assert segment.rev == 0 and segment.state == SegmentState.FINAL
            assert publisher.speaker_snapshot()["detected"] == {"seq": 1, "language": "de"}
    assert len(finals) == 18
