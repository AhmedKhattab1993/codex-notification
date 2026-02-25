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
import uuid
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".codex" / "notify-tts"
QUEUE_FILE = STATE_DIR / "queue.jsonl"
QUEUE_LOCK_FILE = STATE_DIR / "queue.lock"
WORKER_LOCK_FILE = STATE_DIR / "worker.lock"
LOG_FILE = STATE_DIR / "notifier.log"
MODEL_NAME = "tts_models/en/ljspeech/tacotron2-DDC"
MAX_TTS_TEXT_CHARS = 420
MAX_QUEUE_ITEMS = 200
MAX_ITEM_AGE_SECONDS = 1800
TTS_TIMEOUT_SECONDS = 90
AUDIO_TIMEOUT_SECONDS = 45
LAST_SPOKEN_HASH_FILE = STATE_DIR / "last_spoken_hash.txt"


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
    missing = [name for name in ("tts", "afplay") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"Missing required command(s): {', '.join(missing)}")


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
    # Collapse whitespace and clamp length.
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_TTS_TEXT_CHARS:
        text = text[:MAX_TTS_TEXT_CHARS].rstrip() + "."
    if not text:
        raise RuntimeError("Notification text became empty after normalization")
    return text


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

        tmp_prefix = f"{STATE_DIR}/tmp"
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
            if tmp_prefix not in command:
                continue
            try:
                os.kill(int(pid_text), signal.SIGKILL)
                log_line("INFO", f"Killed stale afplay pid={pid_text}")
            except (ValueError, ProcessLookupError, PermissionError):
                continue
    except Exception as exc:  # noqa: BLE001
        log_line("ERROR", f"Failed stale-audio cleanup: {exc}")


def play_tts(text: str) -> None:
    ensure_state_dir()
    temp_wav_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".wav", delete=False, dir=str(STATE_DIR)
        ) as temp_wav:
            temp_wav_path = temp_wav.name

        synth = subprocess.run(
            [
                "tts",
                "--model_name",
                MODEL_NAME,
                "--text",
                text,
                "--out_path",
                temp_wav_path,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=TTS_TIMEOUT_SECONDS,
        )
        if synth.returncode != 0:
            raise RuntimeError(f"tts failed: {synth.stderr.strip()}")

        if not os.path.exists(temp_wav_path) or os.path.getsize(temp_wav_path) == 0:
            raise RuntimeError("tts produced empty audio output")

        player = subprocess.run(
            ["afplay", temp_wav_path],
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


def spawn_worker() -> None:
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


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

    worker_lock_handle = WORKER_LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(worker_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    log_line("INFO", "Worker started")
    try:
        while True:
            entry = pop_queue_fifo()
            if entry is None:
                break

            try:
                if is_stale_entry(entry):
                    log_line("INFO", f"Skipped stale notification {entry.get('id', 'unknown')}")
                    continue

                text = entry.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise RuntimeError(f"Invalid queue entry: {entry}")

                if is_duplicate_of_last_spoken(text):
                    log_line("INFO", f"Skipped duplicate notification {entry.get('id', 'unknown')}")
                    continue

                log_line("INFO", f"Playing notification {entry.get('id', 'unknown')}")
                play_tts(text)
                mark_last_spoken(text)
                log_line("INFO", f"Played notification {entry.get('id', 'unknown')}")
            except subprocess.TimeoutExpired:
                log_line("ERROR", f"Notification timeout {entry.get('id', 'unknown')}")
            except Exception as exc:  # noqa: BLE001
                log_line("ERROR", f"Notification failed {entry.get('id', 'unknown')}: {exc}")
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
