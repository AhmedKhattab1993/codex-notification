#!/usr/bin/env python3
"""Codex notify hook entrypoint with a singleton queue worker."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import re
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".codex" / "notify-tts"
QUEUE_FILE = STATE_DIR / "queue.jsonl"
QUEUE_LOCK_FILE = STATE_DIR / "queue.lock"
WORKER_LOCK_FILE = STATE_DIR / "worker.lock"
LOG_FILE = STATE_DIR / "notifier.log"
TEMP_AUDIO_PREFIX = "notify-tts-"
TEMP_AUDIO_PATTERNS = (f"{TEMP_AUDIO_PREFIX}*.wav", "tmp*.wav")
MODEL_NAME = "Chatterbox-Turbo"
CHATTERBOX_REPO_ID = "ResembleAI/chatterbox-turbo"
CHATTERBOX_DEVICE = os.environ.get("CODEX_CHATTERBOX_DEVICE", "mps").strip().lower()
CHATTERBOX_EXAGGERATION = float(
    os.environ.get("CODEX_CHATTERBOX_EXAGGERATION", "0.5")
)
CHATTERBOX_CFG_WEIGHT = float(os.environ.get("CODEX_CHATTERBOX_CFG_WEIGHT", "0.5"))
CHATTERBOX_AUDIO_PROMPT_PATH = os.environ.get(
    "CODEX_CHATTERBOX_AUDIO_PROMPT_PATH", ""
).strip()
MAX_TTS_CHUNK_CHARS = int(os.environ.get("CODEX_TTS_CHUNK_CHARS", "0"))
MAX_QUEUE_ITEMS = 200
MAX_ITEM_AGE_SECONDS = 1800
AUDIO_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TTS_PLAY_TIMEOUT_SECONDS", "90"))
PLAYBACK_RATE = float(os.environ.get("CODEX_TTS_PLAYBACK_RATE", "1.1"))
WORKER_IDLE_TIMEOUT_SECONDS = int(
    os.environ.get("CODEX_TTS_WORKER_IDLE_SECONDS", "86400")
)
WORKER_POLL_INTERVAL_SECONDS = float(
    os.environ.get("CODEX_TTS_WORKER_POLL_INTERVAL_SECONDS", "0.2")
)
LAST_SPOKEN_HASH_FILE = STATE_DIR / "last_spoken_hash.txt"
_TTS_ENGINE: Any | None = None
_RESOLVED_DEVICE: str | None = None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def log_line(level: str, message: str) -> None:
    ensure_state_dir()
    line = f"{utc_now()} [{level}] {message}\n"
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line)


def require_commands() -> None:
    missing = [name for name in ("afplay",) if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"Missing required command(s): {', '.join(missing)}")
    try:
        import torch  # noqa: F401
        import torchaudio  # noqa: F401
        from huggingface_hub import snapshot_download  # noqa: F401
        from chatterbox.tts_turbo import ChatterboxTurboTTS  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Missing required Python modules for Chatterbox-Turbo"
        ) from exc
    if CHATTERBOX_AUDIO_PROMPT_PATH:
        prompt_path = Path(CHATTERBOX_AUDIO_PROMPT_PATH)
        if not prompt_path.exists() or not prompt_path.is_file():
            raise RuntimeError(
                "CODEX_CHATTERBOX_AUDIO_PROMPT_PATH must point to an existing file"
            )


def queue_lock():
    ensure_state_dir()
    handle = QUEUE_LOCK_FILE.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def append_queue(entry: dict[str, Any]) -> None:
    with queue_lock():
        with QUEUE_FILE.open("a", encoding="utf-8") as queue:
            queue.write(json.dumps(entry, ensure_ascii=True) + "\n")
            queue.flush()
            os.fsync(queue.fileno())

        if not QUEUE_FILE.exists():
            return

        with QUEUE_FILE.open("r+", encoding="utf-8") as queue:
            lines = queue.readlines()
            if len(lines) <= MAX_QUEUE_ITEMS:
                return
            lines = lines[-MAX_QUEUE_ITEMS:]
            queue.seek(0)
            queue.truncate(0)
            queue.writelines(lines)
            queue.flush()
            os.fsync(queue.fileno())


def pop_queue_fifo() -> dict[str, Any] | None:
    with queue_lock():
        if not QUEUE_FILE.exists():
            return None

        with QUEUE_FILE.open("r+", encoding="utf-8") as queue:
            lines = queue.readlines()
            if not lines:
                return None

            first_line = lines[0].rstrip("\n")
            try:
                entry = json.loads(first_line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Malformed queue entry: {first_line}") from exc

            queue.seek(0)
            queue.truncate(0)
            queue.writelines(lines[1:])
            queue.flush()
            os.fsync(queue.fileno())
            return entry


def extract_text(payload: Any) -> str:
    if isinstance(payload, str):
        text = payload.strip()
        if text:
            return text
        raise RuntimeError("Payload string is empty")

    if isinstance(payload, dict):
        # Primary path for Codex notify payloads.
        for key in ("last-assistant-message", "message", "text", "content", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise RuntimeError(
            "Payload must contain one of: last-assistant-message, message, text, content, title"
        )

    raise RuntimeError("Payload must be a JSON object or string")


def normalize_text_for_tts(text: str) -> str:
    text = text.strip()
    if not text:
        raise RuntimeError("Notification text is empty")

    # Convert markdown links to just their label.
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Drop inline code and heading markers for cleaner speech.
    text = text.replace("`", "")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    # Strip obvious path/ID/hash style noise.
    text = re.sub(r"/Users/[^\s)]+", "", text)
    text = re.sub(r"\b[0-9a-f]{7,40}\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "", text, flags=re.IGNORECASE)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise RuntimeError("Notification text became empty after normalization")
    return text


def split_text_for_tts(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        return [text]
    if max_chars < 80:
        raise RuntimeError("CODEX_TTS_CHUNK_CHARS must be at least 80")
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        window = remaining[:max_chars]
        split_idx = -1

        # Prefer sentence boundaries.
        for punct in (". ", "! ", "? ", "; ", ": "):
            idx = window.rfind(punct)
            if idx > split_idx:
                split_idx = idx + len(punct)

        # Otherwise split on last whitespace inside the window.
        if split_idx == -1:
            split_idx = window.rfind(" ")

        # Fail-safe hard split for long tokens without spaces.
        if split_idx <= 0:
            split_idx = max_chars

        chunk = remaining[:split_idx].strip()
        if not chunk:
            break
        chunks.append(chunk)
        remaining = remaining[split_idx:].strip()

    return chunks


def _message_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_duplicate_of_last_spoken(text: str) -> bool:
    try:
        if not LAST_SPOKEN_HASH_FILE.exists():
            return False
        previous = LAST_SPOKEN_HASH_FILE.read_text(encoding="utf-8").strip()
        return previous == _message_hash(text)
    except OSError:
        return False


def mark_last_spoken(text: str) -> None:
    ensure_state_dir()
    LAST_SPOKEN_HASH_FILE.write_text(_message_hash(text), encoding="utf-8")


def is_stale_entry(entry: dict[str, Any]) -> bool:
    queued_at = entry.get("queued_at")
    if not isinstance(queued_at, str):
        return False
    try:
        queued_time = dt.datetime.fromisoformat(queued_at)
    except ValueError:
        return False
    now = dt.datetime.now(dt.timezone.utc)
    return (now - queued_time).total_seconds() > MAX_ITEM_AGE_SECONDS


def kill_stray_notify_audio() -> None:
    """Kill orphaned afplay processes that are playing notifier temp WAV files."""
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return

        state_dir_str = str(STATE_DIR)
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            pid_text, command = parts
            if not command.startswith("afplay "):
                continue
            if state_dir_str not in command or ".wav" not in command:
                continue
            try:
                os.kill(int(pid_text), signal.SIGKILL)
                log_line("INFO", f"Killed stale afplay pid={pid_text}")
            except (ValueError, ProcessLookupError, PermissionError):
                continue
    except Exception as exc:  # noqa: BLE001
        log_line("ERROR", f"Failed stale-audio cleanup: {exc}")


def cleanup_stale_temp_audio_files() -> None:
    ensure_state_dir()
    removed = 0
    for pattern in TEMP_AUDIO_PATTERNS:
        for path in STATE_DIR.glob(pattern):
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001
                log_line("ERROR", f"Failed removing stale temp audio file {path}: {exc}")
    if removed:
        log_line("INFO", f"Removed stale temp audio files: {removed}")


def resolve_chatterbox_device() -> str:
    import torch

    if CHATTERBOX_DEVICE == "mps":
        if not hasattr(torch.backends, "mps") or not torch.backends.mps.is_available():
            raise RuntimeError(
                "CODEX_CHATTERBOX_DEVICE is set to mps, but MPS is not available on this machine"
            )
        return "mps"
    if CHATTERBOX_DEVICE == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CODEX_CHATTERBOX_DEVICE is set to cuda, but CUDA is not available on this machine"
            )
        return "cuda"
    if CHATTERBOX_DEVICE == "cpu":
        return "cpu"
    if CHATTERBOX_DEVICE != "auto":
        raise RuntimeError(
            "CODEX_CHATTERBOX_DEVICE must be one of: auto, cpu, mps, cuda"
        )

    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def get_chatterbox_checkpoint_dir() -> Path:
    from huggingface_hub import snapshot_download

    cache_dir = STATE_DIR / "models" / "chatterbox-turbo"
    cache_dir.mkdir(parents=True, exist_ok=True)
    allow_patterns = [
        "ve.safetensors",
        "t3_turbo_v1.safetensors",
        "s3gen_meanflow.safetensors",
        "conds.pt",
        "vocab.json",
        "merges.txt",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "added_tokens.json",
        "tokenizer.json",
    ]
    try:
        local_path = snapshot_download(
            repo_id=CHATTERBOX_REPO_ID,
            token=False,
            local_dir=cache_dir,
            allow_patterns=allow_patterns,
        )
    except TypeError:
        local_path = snapshot_download(
            repo_id=CHATTERBOX_REPO_ID,
            token=False,
            allow_patterns=allow_patterns,
        )
    return Path(local_path)


def get_tts_engine() -> Any:
    global _TTS_ENGINE, _RESOLVED_DEVICE
    if _TTS_ENGINE is not None:
        return _TTS_ENGINE
    try:
        import perth
        from chatterbox.tts_turbo import ChatterboxTurboTTS
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Failed to import Chatterbox-Turbo module") from exc

    try:
        if getattr(perth, "PerthImplicitWatermarker", None) is None:
            if getattr(perth, "DummyWatermarker", None) is None:
                raise RuntimeError("No Perth watermarker implementation is available")
            perth.PerthImplicitWatermarker = perth.DummyWatermarker
            log_line(
                "INFO",
                "Perth implicit watermark unavailable; using dummy watermarker",
            )
        _RESOLVED_DEVICE = resolve_chatterbox_device()
        ckpt_dir = get_chatterbox_checkpoint_dir()
        _TTS_ENGINE = ChatterboxTurboTTS.from_local(ckpt_dir=ckpt_dir, device=_RESOLVED_DEVICE)
        log_line("INFO", f"Loaded {MODEL_NAME} on device={_RESOLVED_DEVICE}")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed loading {MODEL_NAME}: {exc}") from exc

    return _TTS_ENGINE


def synthesize_and_play_chunk(text: str) -> None:
    ensure_state_dir()
    temp_wav_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".wav",
            prefix=TEMP_AUDIO_PREFIX,
            delete=False,
            dir=str(STATE_DIR),
        ) as temp_wav:
            temp_wav_path = temp_wav.name

        import torchaudio as ta

        tts_engine = get_tts_engine()
        synth_args: dict[str, Any] = {
            "text": text,
            "exaggeration": CHATTERBOX_EXAGGERATION,
            "cfg_weight": CHATTERBOX_CFG_WEIGHT,
        }
        if CHATTERBOX_AUDIO_PROMPT_PATH:
            synth_args["audio_prompt_path"] = CHATTERBOX_AUDIO_PROMPT_PATH

        wav = tts_engine.generate(**synth_args)
        if wav is None:
            raise RuntimeError("Chatterbox-Turbo returned empty waveform")
        if getattr(wav, "dim", lambda: 0)() == 1:
            wav = wav.unsqueeze(0)
        if wav.dim() != 2:
            raise RuntimeError("Chatterbox-Turbo returned malformed waveform tensor")
        ta.save(temp_wav_path, wav.detach().cpu(), tts_engine.sr)

        if not os.path.exists(temp_wav_path) or os.path.getsize(temp_wav_path) == 0:
            raise RuntimeError("tts produced empty audio output")

        player = subprocess.run(
            ["afplay", "-r", str(PLAYBACK_RATE), temp_wav_path],
            check=False,
            capture_output=True,
            text=True,
            timeout=AUDIO_TIMEOUT_SECONDS,
        )
        if player.returncode != 0:
            raise RuntimeError(f"afplay failed: {player.stderr.strip()}")
    finally:
        if temp_wav_path and os.path.exists(temp_wav_path):
            os.remove(temp_wav_path)


def play_tts(text: str) -> None:
    chunks = split_text_for_tts(text, MAX_TTS_CHUNK_CHARS)
    for idx, chunk in enumerate(chunks, start=1):
        if len(chunks) > 1:
            log_line("INFO", f"TTS chunk {idx}/{len(chunks)}")
        synthesize_and_play_chunk(chunk)


def spawn_worker() -> None:
    if is_worker_running():
        return
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def is_worker_running() -> bool:
    ensure_state_dir()
    handle = WORKER_LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return True
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        return False


def run_notify_mode(payload_arg: str) -> int:
    require_commands()
    try:
        payload = json.loads(payload_arg)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid JSON payload argument") from exc

    text = normalize_text_for_tts(extract_text(payload))
    entry = {
        "id": str(uuid.uuid4()),
        "queued_at": utc_now(),
        "text": text,
    }
    append_queue(entry)
    log_line("INFO", f"Enqueued notification {entry['id']}")
    spawn_worker()
    return 0


def run_worker_mode() -> int:
    require_commands()
    ensure_state_dir()
    kill_stray_notify_audio()
    cleanup_stale_temp_audio_files()
    if WORKER_IDLE_TIMEOUT_SECONDS < 1:
        raise RuntimeError("CODEX_TTS_WORKER_IDLE_SECONDS must be at least 1")
    if WORKER_POLL_INTERVAL_SECONDS <= 0:
        raise RuntimeError("CODEX_TTS_WORKER_POLL_INTERVAL_SECONDS must be > 0")
    if PLAYBACK_RATE <= 0:
        raise RuntimeError("CODEX_TTS_PLAYBACK_RATE must be > 0")

    worker_lock_handle = WORKER_LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(worker_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    log_line("INFO", "Worker started")
    log_line("INFO", "Pre-warming TTS engine")
    get_tts_engine()
    log_line("INFO", "TTS engine is warm")
    last_active_monotonic = time.monotonic()
    try:
        while True:
            entry = pop_queue_fifo()
            if entry is None:
                idle_seconds = time.monotonic() - last_active_monotonic
                if idle_seconds >= WORKER_IDLE_TIMEOUT_SECONDS:
                    log_line("INFO", "Worker idle timeout reached; stopping")
                    break
                time.sleep(WORKER_POLL_INTERVAL_SECONDS)
                continue

            try:
                if is_stale_entry(entry):
                    log_line("INFO", f"Skipped stale notification {entry.get('id', 'unknown')}")
                    last_active_monotonic = time.monotonic()
                    continue

                text = entry.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise RuntimeError(f"Invalid queue entry: {entry}")

                if is_duplicate_of_last_spoken(text):
                    log_line("INFO", f"Skipped duplicate notification {entry.get('id', 'unknown')}")
                    last_active_monotonic = time.monotonic()
                    continue

                log_line("INFO", f"Playing notification {entry.get('id', 'unknown')}")
                play_tts(text)
                mark_last_spoken(text)
                log_line("INFO", f"Played notification {entry.get('id', 'unknown')}")
                last_active_monotonic = time.monotonic()
            except subprocess.TimeoutExpired:
                log_line("ERROR", f"Notification timeout {entry.get('id', 'unknown')}")
                last_active_monotonic = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                log_line("ERROR", f"Notification failed {entry.get('id', 'unknown')}: {exc}")
                last_active_monotonic = time.monotonic()
        return 0
    finally:
        log_line("INFO", "Worker stopped")
        worker_lock_handle.close()


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[1] == "--worker":
            return run_worker_mode()

        if len(argv) != 2:
            raise RuntimeError("Usage: codex_tts_notify.py '<json_payload>'")

        return run_notify_mode(argv[1])
    except Exception as exc:  # noqa: BLE001
        log_line("ERROR", str(exc))
        print(f"codex-tts-notify error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
