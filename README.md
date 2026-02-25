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
- Notifications are appended to a queue at `~/.codex/notify-tts/queue.jsonl`.
- A detached singleton worker drains the queue in FIFO order.
- Logs are written to `~/.codex/notify-tts/notifier.log`.
- First playback can take several minutes while Coqui downloads/extracts the model; later turns are much faster.
