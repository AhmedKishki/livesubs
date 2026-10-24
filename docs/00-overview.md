# Application scope

## What this is

- A server captures English/German speech from one shared microphone and publishes subtitles in each attendee's language.
- Attendees open the viewer page by scanning a QR code on the shared network.
- Capture runs on the server. The viewer page asks for no microphone permission, so it works over plain HTTP.

## Requirements

| Requirement | Detail |
| --- | --- |
| Python and uv | Python 3.12 or newer, with dependencies installed by uv. |
| Live microphone capture | Requires a Linux host running PipeWire, with `pw-record` and `pw-dump`. |
| An audio source | A PipeWire microphone, a 16-bit PCM WAV file, or synthetic test audio. |
| Outbound HTTPS | The shipped live providers process audio and text through APIs. |
| A shared network | Attendees must be able to reach the server from their phones. |
| Provider credentials | Live recognition and hosted translation need keys. Simulated modes do not. |

- API-based processing needs no local GPU. A local OpenAI-compatible translator can also be configured, with performance dependent on the model and hardware.
- Audio sources and providers can be replaced without changing the viewer's message format.

## Languages

- Every session offers English, German and Arabic subtitles. The recognition setting is independent: Automatic detects English/German turns, while English and German pin the recogniser to one language. Startup and the operator console offer both automatic behaviour and a manual override.
- Each attendee reads one language throughout the conversation. A wholly same-language commitment uses transcription for its matching reader; mixed or unknown commitments require translation for every demanded target. [Segmentation](03-segmentation.md) defines the language-evidence rules.
- A plain QR scan selects a supported phone language, otherwise the operator's fallback. Visible language buttons override it. [Viewer controls](06-overlay-spec.md) define URL precedence and reconnect behaviour.
- The server sends each viewer only that language's output and strips raw-source fields, word timings and other targets. A transcription can be the viewer's own output text; raw-source access remains operator-only.
- The source text stays in the pipeline and reaches the operator console, so a mistranslation can be diagnosed.
- Recognition is shared while viewers exist. Translation runs only for demanded targets that need it. [Configuration](12-configuration.md) defines supported settings and migration.

## Non-goals

- The shipped approach does not include local speech recognition.
- The application does not combine independent microphones for beamforming.
- The viewer cannot overlay another application on a projector.
- Speaker diarization, audience Q&A and speech synthesis are not supported.
- Speakers of languages other than English and German are not supported. Arabic is a subtitle language only.
- The service has no accounts or authentication. Saved audio and subtitle files are optional, but there is no persistent session database.
