# Application scope

## What this is

- A server captures one presenter's speech and publishes subtitles in the listener's language to attendees' phones.
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

## Language modes

- The speaker language selects the mode. An English speaker has German and Arabic listeners, and a German speaker has English and Arabic listeners. A conversation whose speakers switch between English and German, `en+de`, has English, German and Arabic listeners. The mode is chosen at startup with `--speaker`, `LIVESUBS_SESSION_SPEAKER` or `session.speaker`.
- The default viewer receives the mode's first target: German for an English speaker, English for a German speaker or a conversation. Every other target needs the viewer's explicit choice.
- In a conversation each viewer reads one language, whoever is speaking. Every segment goes to the translator of every language with readers, including the language it was spoken in, so a viewer never receives the transcript. A conversation uses one shared microphone, so the speech of people talking at once is not separated.
- The server sends each viewer one language only and strips the speaker's own words, so filtering never depends on the viewer page.
- The source text stays in the pipeline and reaches the operator console, so a mistranslation can be diagnosed.
- `session.targets` can narrow a mode to some of its targets. Adding a language or a mode is a registry entry described in [11-extending](11-extending.md).

## Non-goals

- The shipped approach does not include local speech recognition.
- The application does not combine independent microphones for beamforming.
- The viewer cannot overlay another application on a projector.
- Speaker diarization, audience Q&A and speech synthesis are not supported.
- Speakers of languages other than English and German are not supported. Arabic is a subtitle language only.
- The service has no accounts or authentication. Saved audio and subtitle files are optional, but there is no persistent session database.
