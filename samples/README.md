# Corpus

- The corpus uses a podcast episode, its published English transcript and a German translation to test real speech.
- The media is not licensed for redistribution by this project. Inputs and derived files are untracked and must be supplied locally.

## Build it

- Supply the source files at the paths in the command, or change the arguments to match your copies.
- Preparation needs `gst-launch-1.0` (GStreamer) and `pdftotext` (poppler). Neither is an application runtime dependency, and `ffmpeg` is not needed.

```bash
uv run python tools/prepare_corpus.py \
  --audio 'samples/English in 10 Minutes: Episode 0: Introduction.mp3' \
  --transcript 'samples/English in 10 Minutes: Episode 0: Introduction.pdf' \
  --transcript-de 'samples/English in 10 Minutes_ Episode 0_ Introduction de-DE.pdf' \
  --out-dir samples --stem sample
```

- Preparation writes replay audio, reference text and a manifest under `samples/`.
- The manifest includes source SHA-256 digests, WAV duration, word counts and unique-word counts so results can identify their inputs.

## Use it

- Run the metric self-test without a provider or network:

```bash
uv run python tools/verify_against_reference.py --reference samples/reference.en.txt --self-test
```

- Replay the audio path without credentials. Stub words exercise processing and segmentation, not recognition accuracy:

```bash
uv run livesubs --serve --approach live-sim --audio file:samples/sample-16k-mono.wav@0
```

- Score a session's output using the actual generated filenames in place of the example hypothesis paths:

```bash
uv run python tools/verify_against_reference.py \
  --reference samples/reference.en.txt \
  --hypothesis recordings/sample.en.srt \
  --hypothesis-de recordings/sample.de.srt \
  --reference-de samples/reference.de.txt
```

### Align the transcript and translate it live

- Generate cue spans with `live-sim`, stop its server with Ctrl-C after playback, then align the reference and run `live-text`.
- Replace `recordings/sample.en.srt` with the generated English sidecar. The application includes the session and timestamp in its real filename.
- The alignment tool distributes reference words in proportion to cue durations. It does not recognise words or verify utterance boundaries.

```bash
# 1. Produce a timed cue file. Its spans are real speech boundaries.
uv run livesubs --serve --approach live-sim --audio file:samples/sample-16k-mono.wav@0

# 2. Lay the transcript onto those spans.
uv run python tools/align_reference.py \
  --reference samples/reference.en.txt \
  --timing recordings/sample.en.srt \
  --out samples/reference.en.timed.srt

# 3. Translate the timed transcript live, against the audio.
uv run livesubs --serve --approach live-text \
  --audio file:samples/sample-16k-mono.wav@1 --cues samples/reference.en.timed.srt
```

- Play audio in a second terminal to read along:

```bash
gst-play-1.0 samples/sample-16k-mono.wav
```

- Start the player within about a second of the pipeline's readiness message, not the initial process launch. Manual playback timing is approximate.
- Alignment is suitable for rehearsal, not word-timing scores. Incorrect spans or omitted speech, such as the unmatched intro, can place reference words in the wrong utterances.
- `live-text` needs a translator but no recognition key. A local translator declared with `requires_key: false` also works, with speed dependent on its model and hardware.
- Use a supported audio-processing mode to produce cue spans. `sim` has no audio clock or subtitle sidecars.

## What it can and cannot prove

- The audio has one speaker in a studio, no audience and about 120 words per minute. It exercises processing, segmentation and recognition, but not far-field lecture-hall conditions.

- The publisher's English transcript is treated as ground truth but omits the produced intro ("Welcome to English… Real, unscripted English conversation… visit englishin10minutes.com"). The first roughly 30 s therefore has no reference.
- Exclude that intro from the scored hypothesis and from alignment spans, or its words count as insertions and shift the timed reference.

- The German reference is an external DeepL translation, not reviewed ground truth. chrF measures agreement with that translation, not correctness.

- The German document sets footnote markers off with spaces (`Prüfer für Englisch 1`) rather than gluing them to a word, so they cannot be stripped without also stripping spoken numerals (`10 Minuten` has the same shape). They stay, which is why the German reference counts 36 numerals against the English 26. Do not compare those counts.
- The two transcripts have 19 and 20 paragraphs, because a page break in the German falls mid-sentence. Paragraph structure is not scored, so this is harmless, but the two cannot be compared paragraph by paragraph.

## Reference measurements

- The recorded comparison covers 594 s of speech:

| | English | German |
| --- | --- | --- |
| Words | 1205 | 1198 (0.994x) |
| Characters | 5877 | 6587 (1.121x) |
| Characters per word | 4.88 | 5.50 |

- This sample's German reference has about 12% more characters. The renderer fits measured pixels, so the ratio is not a general width requirement.
