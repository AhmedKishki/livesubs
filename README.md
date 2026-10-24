# Live translated subtitles

`livesubs` puts English, German and Arabic subtitles on attendees' phones during meetings, conversations and events. It recognises English/German speaker changes automatically on one shared microphone. For Arabic or another source language, select a manual speaker language supported by the configured Deepgram model. Each attendee keeps reading one of the three subtitle languages throughout the conversation.

PipeWire captures the microphone on a Linux machine. Deepgram transcribes the speech, DeepSeek translates it where needed, and the web server sends subtitles to phone browsers. Readers of a wholly same-language segment receive its transcription without a translation request.

## Installation

On Ubuntu/Debian, install the system tools:

```bash
sudo apt update
sudo apt install git curl ca-certificates pipewire pipewire-bin wireplumber alsa-utils iproute2 iputils-ping
systemctl --user enable --now pipewire.socket pipewire.service wireplumber.service
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

Install the application:

```bash
git clone https://github.com/AhmedKishki/livesubs
cd livesubs
uv tool install .
uv tool update-shell
```

## First session

Open a new terminal and run this from any directory:

```bash
livesubs
```

1. Choose Automatic recognition or a supported manual speaker language, such as Arabic, and select the fallback subtitle language.
2. Choose your microphone with the arrow keys and press Enter.
3. Enter your [Deepgram](https://console.deepgram.com/) and [DeepSeek](https://platform.deepseek.com/api_keys) API keys when prompted. They are saved for later sessions.
4. Speak into the microphone while startup checks the signal.
5. Open the printed landing-page URL and display its QR code.
6. Attendees join the same trusted network and scan the code. Their phone's supported language is selected automatically, otherwise the operator's fallback is used. English, Deutsch and العربية buttons change the language with one tap.

Open `/admin` on the server's address to change recognition or the fallback during the session. Leave recognition on Automatic when English and German speakers take turns. The manual list follows the configured model, including its supported regional variants. A manual change briefly reopens recognition and can miss speech, so make it during a pause. Live choices last for the current run only.

Recognition is shared between languages. Translation runs only for languages with readers, and all paid processing pauses when the last viewer disconnects. Keep the unauthenticated service on a trusted LAN and the terminal running during the session. Press Ctrl-C to stop.
