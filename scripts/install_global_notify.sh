#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_SCRIPT="${SCRIPT_DIR}/codex_tts_notify.py"

CODEX_DIR="${HOME}/.codex"
BIN_DIR="${CODEX_DIR}/bin"
VOICE_DIR="${CODEX_DIR}/voice-notify"
VENV_DIR="${VOICE_DIR}/venv"
CONFIG_FILE="${CODEX_DIR}/config.toml"

NOTIFY_CMD="/Users/ahmedkhattab/.codex/bin/codex-tts-notify"
TARGET_SCRIPT="${BIN_DIR}/codex-tts-notify.py"
TARGET_WRAPPER="${BIN_DIR}/codex-tts-notify"

if [[ ! -f "${SOURCE_SCRIPT}" ]]; then
  echo "Missing source notifier script: ${SOURCE_SCRIPT}" >&2
  exit 1
fi

mkdir -p "${BIN_DIR}" "${VOICE_DIR}"

if ! command -v uv >/dev/null 2>&1; then
  echo "Missing dependency: uv (required to install Python 3.11 and Chatterbox-Turbo)" >&2
  exit 1
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  uv python install 3.11
  rm -rf "${VENV_DIR}"
  uv venv --python 3.11 "${VENV_DIR}"
fi

uv pip install --python "${VENV_DIR}/bin/python" --upgrade pip setuptools wheel
uv pip install --python "${VENV_DIR}/bin/python" --upgrade "torch==2.10.0" "torchaudio==2.10.0"
uv pip install --python "${VENV_DIR}/bin/python" --upgrade \
  "transformers==4.57.6" \
  "diffusers==0.29.0" \
  "huggingface-hub>=0.24,<1" \
  "librosa==0.11.0" \
  "soundfile==0.13.1" \
  "omegaconf==2.3.0" \
  "resemble-perth==1.0.1" \
  "s3tokenizer==0.3.0" \
  "conformer==0.3.2"
uv pip install --python "${VENV_DIR}/bin/python" --upgrade --no-deps "chatterbox-tts==0.1.6"

cp "${SOURCE_SCRIPT}" "${TARGET_SCRIPT}"
chmod 755 "${TARGET_SCRIPT}"

cat > "${TARGET_WRAPPER}" <<'WRAPPER_EOF'
#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.codex/voice-notify/venv/bin:$PATH"
export CODEX_CHATTERBOX_DEVICE="${CODEX_CHATTERBOX_DEVICE:-mps}"
export CODEX_CHATTERBOX_EXAGGERATION="${CODEX_CHATTERBOX_EXAGGERATION:-0.5}"
export CODEX_CHATTERBOX_CFG_WEIGHT="${CODEX_CHATTERBOX_CFG_WEIGHT:-0.5}"
export CODEX_CHATTERBOX_AUDIO_PROMPT_PATH="${CODEX_CHATTERBOX_AUDIO_PROMPT_PATH:-}"
export CODEX_TTS_CHUNK_CHARS="${CODEX_TTS_CHUNK_CHARS:-0}"
export CODEX_TTS_WORKER_IDLE_SECONDS="${CODEX_TTS_WORKER_IDLE_SECONDS:-86400}"
export CODEX_TTS_WORKER_POLL_INTERVAL_SECONDS="${CODEX_TTS_WORKER_POLL_INTERVAL_SECONDS:-0.2}"
export CODEX_TTS_PLAYBACK_RATE="${CODEX_TTS_PLAYBACK_RATE:-1.1}"
exec "$HOME/.codex/voice-notify/venv/bin/python" "$HOME/.codex/bin/codex-tts-notify.py" "$@"
WRAPPER_EOF
chmod 755 "${TARGET_WRAPPER}"

mkdir -p "$(dirname "${CONFIG_FILE}")"
if [[ ! -f "${CONFIG_FILE}" ]]; then
  touch "${CONFIG_FILE}"
fi

NOTIFY_LINE="notify=[\"${NOTIFY_CMD}\"]"
TMP_FILE="$(mktemp "${CONFIG_FILE}.tmp.XXXXXX")"

awk -v notify_line="${NOTIFY_LINE}" '
BEGIN {
  print notify_line
}
/^[[:space:]]*notify[[:space:]]*=/ { next }
{
  print
}
' "${CONFIG_FILE}" > "${TMP_FILE}"

mv "${TMP_FILE}" "${CONFIG_FILE}"

echo "Installed global Codex Chatterbox-Turbo notifier."
echo "Wrapper: ${TARGET_WRAPPER}"
echo "Config updated: ${CONFIG_FILE}"
