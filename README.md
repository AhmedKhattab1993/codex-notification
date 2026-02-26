# codex-notification

Install global notifier:

```bash
./scripts/install_global_notify.sh
```

Test it:

```bash
/Users/ahmedkhattab/.codex/bin/codex-tts-notify '{"message":"Codex TTS notifier is ready"}'
```

Notes:
- Global Codex hook is set in `~/.codex/config.toml` as `notify=["/Users/ahmedkhattab/.codex/bin/codex-tts-notify"]`.
- Default model is local `Chatterbox-Turbo` on `mps`.
- Notifications are appended to a queue at `~/.codex/notify-tts/queue.jsonl`.
- A detached singleton worker drains the queue in FIFO order.
- Logs are written to `~/.codex/notify-tts/notifier.log`.
- Worker stays warm by default for 24 hours to avoid model reload overhead.
- Long messages are split into speech-safe chunks by default (`CODEX_TTS_CHUNK_CHARS=260`) so full text is spoken reliably.
- Polling defaults to `0.2s` for faster pickup (`CODEX_TTS_WORKER_POLL_INTERVAL_SECONDS`).
- Playback defaults to `1.1x` (`CODEX_TTS_PLAYBACK_RATE`) for shorter listen time without truncation.
- First playback can take longer while Chatterbox-Turbo downloads model files; later turns are much faster.
