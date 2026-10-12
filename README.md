# Live translated subtitles

`livesubs` turns source-language speech into target-language subtitles displayed on attendees' phones. Currently, it supports **English speech → German subtitles**.

PipeWire captures the microphone on a Linux machine. Deepgram transcribes the speech, DeepSeek translates the text, and the web server sends the subtitles to phone browsers.

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

1. Choose your microphone with the arrow keys and press Enter.
2. Enter your [Deepgram](https://console.deepgram.com/) and [DeepSeek](https://platform.deepseek.com/api_keys) API keys when prompted. They are saved for later sessions.
3. Speak into the microphone while startup checks the signal.
4. Open the printed landing-page URL and display its QR code.
5. Attendees join the same network and scan the code to read German subtitles on their phones.

Transcription and translation run while a viewer is connected and pause when the last viewer disconnects. Keep the terminal running during the session. Press Ctrl-C to stop.

## Planned language support

English → Arabic, German → English, and German → Arabic are planned, not yet implemented.
