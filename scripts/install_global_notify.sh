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
  echo "Missing dependency: uv (required to install Python 3.11 and Coqui TTS)" >&2
  exit 1
fi

if [[ ! -x "${VENV_DIR}/bin/python" || ! -x "${VENV_DIR}/bin/tts" ]]; then
  uv python install 3.11
  rm -rf "${VENV_DIR}"
  uv venv --python 3.11 "${VENV_DIR}"
fi

uv pip install --python "${VENV_DIR}/bin/python" --upgrade torch torchaudio --index-url https://download.pytorch.org/whl/cpu
uv pip install --python "${VENV_DIR}/bin/python" --upgrade "coqui-tts[codec]" "transformers>=4.57,<5"

cp "${SOURCE_SCRIPT}" "${TARGET_SCRIPT}"
chmod 755 "${TARGET_SCRIPT}"

cat > "${TARGET_WRAPPER}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.codex/voice-notify/venv/bin:$PATH"
exec "$HOME/.codex/voice-notify/venv/bin/python" "$HOME/.codex/bin/codex-tts-notify.py" "$@"
EOF
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

echo "Installed global Codex TTS notifier."
echo "Wrapper: ${TARGET_WRAPPER}"
echo "Config updated: ${CONFIG_FILE}"
