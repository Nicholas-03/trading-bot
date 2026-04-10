# Colored Logging via Rich

**Date:** 2026-04-10
**Status:** Approved

## Problem

The bot's log output is plain monochrome text, making it hard to visually distinguish log levels (INFO vs WARNING vs ERROR) at a glance during paper trading sessions.

## Solution

Replace `logging.basicConfig` in `main.py` with `RichHandler` from the `rich` library. All other modules use `logging.getLogger(__name__)` and require no changes — they inherit the root handler automatically.

## Changes

### `requirements.txt`
Add `rich>=13.0.0`.

### `main.py`
Replace:
```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
```

With:
```python
from rich.logging import RichHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
```

## Behavior

- `INFO` = green, `WARNING` = yellow, `ERROR` = red level badges
- Timestamps dimmed on the left in `[HH:MM:SS]` format
- Logger name (`%(name)s`) included in the message column via existing format strings
- `rich_tracebacks=True`: the five `logger.exception()` calls get colorized tracebacks with local variable values
- `show_path=False`: suppresses the redundant file:line suffix

## Scope

Two files only: `requirements.txt` and `main.py`. No test changes needed.
