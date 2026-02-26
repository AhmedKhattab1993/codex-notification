# codex-notification

Install global notifier:

```bash
./scripts/install_global_notify.sh
```

Test it:

```bash
/Users/ahmedkhattab/.codex/bin/codex-tts-notify '{"message":"Codex TTS notifier is ready"}'
```

Recommended final-response format for voice + detail:

```markdown
## TTS Summary
codex-notification successful. Short spoken summary goes here.

## Details
- Full implementation details for reading.
- Commands, tradeoffs, and technical notes.
```

Behavior:
- If `## TTS Summary` exists and has content, only that section is spoken.
- If `## TTS Summary` is missing or empty, the notifier falls back to the full message after normalization.
- `## Details` is preserved for full text context and is not spoken when a valid summary exists.

Run tests:

```bash
python3 -m unittest discover -s tests
```

Notes:
- Global Codex hook is set in `~/.codex/config.toml` as `notify=["/Users/ahmedkhattab/.codex/bin/codex-tts-notify"]`.
- Default model is local `Chatterbox-Turbo` on `mps`.
- Notifications are appended to a queue at `~/.codex/notify-tts/queue.jsonl`.
- A detached singleton worker drains the queue in FIFO order.
- Logs are written to `~/.codex/notify-tts/notifier.log`.
- Worker stays warm by default for 24 hours to avoid model reload overhead.
- Long messages are split into speech-safe chunks by default (`CODEX_TTS_CHUNK_CHARS=260`) so full text is spoken reliably.
- Chunk inference is pipelined with playback so next chunk is synthesized while current chunk is playing.
- Polling defaults to `0.2s` for faster pickup (`CODEX_TTS_WORKER_POLL_INTERVAL_SECONDS`).
- Playback defaults to `1.1x` (`CODEX_TTS_PLAYBACK_RATE`) for shorter listen time without truncation.
- First playback can take longer while Chatterbox-Turbo downloads model files; later turns are much faster.
