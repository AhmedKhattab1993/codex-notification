# AGENTS.md

## End-of-Turn Message Style
- Address the user as Ahmed.
- First sentence must include project name and turn state (`successful` or `follow-up needed`).
- Final responses must always have exactly two top-level sections in this order:
- `## TTS Summary`
- `## Details`
- `TTS Summary` is written for speech output.
- `Details` contains complete technical context for reading.
- Subagent behavior remains unchanged.
