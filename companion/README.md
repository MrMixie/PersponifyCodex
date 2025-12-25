# Headless Companion (Skeleton)

This is a minimal, local-only headless companion scaffold. It provides:
- A config schema (`config.schema.json`)
- A basic adapter registry (`adapters/`)
- A small CLI for testing (`companion/cli.py`)

Example run:
```
python3 -m companion.cli --config companion/config.example.json --prompt "hello"
```
