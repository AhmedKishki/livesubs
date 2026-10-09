# Application scope

## What this is

- A server captures one presenter's English speech and publishes German subtitles to attendees on their own phones.
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

## Language direction

- Input is English audio. The viewer page renders German only.
- The English source stays in the pipeline and reaches the operator console, so a mistranslation can be diagnosed.

## Non-goals

- The shipped approach does not include local speech recognition.
- The application does not combine independent microphones for beamforming.
- The viewer cannot overlay another application on a projector.
- Speaker diarization, audience Q&A and German speech synthesis are not supported.
- The service has no accounts or authentication. Saved audio and subtitle files are optional, but there is no persistent session database.
