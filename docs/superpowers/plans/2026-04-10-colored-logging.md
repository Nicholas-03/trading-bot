# Colored Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace plain-text logging with color-coded output using the `rich` library.

**Architecture:** Install `RichHandler` as the single root logging handler in `main.py`. All modules already use `logging.getLogger(__name__)` and inherit the root handler automatically — no changes needed in any other file.

**Tech Stack:** Python `logging` stdlib, `rich>=13.0.0`

---

### Task 1: Add `rich` dependency and wire up `RichHandler`

**Files:**
- Modify: `requirements.txt`
- Modify: `main.py`

- [ ] **Step 1: Add `rich` to `requirements.txt`**

Open `requirements.txt`. It currently reads:

```
# Requires Python 3.11+
alpaca-py>=0.38.0,<1.0.0
anthropic>=0.50.0
google-genai>=1.0.0
python-dotenv>=1.0.0
pytest>=8.0.0
```

Add `rich>=13.0.0` after `python-dotenv`:

```
# Requires Python 3.11+
alpaca-py>=0.38.0,<1.0.0
anthropic>=0.50.0
google-genai>=1.0.0
python-dotenv>=1.0.0
rich>=13.0.0
pytest>=8.0.0
```

- [ ] **Step 2: Install the new dependency**

```bash
pip install rich>=13.0.0
```

Expected: `Successfully installed rich-...` (or "already satisfied" if already present).

- [ ] **Step 3: Replace `logging.basicConfig` in `main.py`**

Current `main.py` top (lines 1–14):

```python
import asyncio
import logging
from config import load_config, Config
from order_executor import OrderExecutor
from llm_advisor import LLMAdvisor
from news_handler import NewsHandler
from position_monitor import PositionMonitor
from alpaca.trading.client import TradingClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)
```

Replace with:

```python
import asyncio
import logging
from rich.logging import RichHandler
from config import load_config, Config
from order_executor import OrderExecutor
from llm_advisor import LLMAdvisor
from news_handler import NewsHandler
from position_monitor import PositionMonitor
from alpaca.trading.client import TradingClient

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s — %(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Verify imports are clean**

```bash
python -c "import main; print('OK')"
```

Expected output: `OK`

- [ ] **Step 5: Verify existing tests still pass**

```bash
python -m pytest tests/ -v
```

Expected: all 13 tests pass (8 in `test_llm_advisor.py`, 5 in `test_position_monitor.py`).

- [ ] **Step 6: Smoke-check the colored output**

```bash
python -c "
import logging
from rich.logging import RichHandler
logging.basicConfig(level=logging.DEBUG, format='%(name)s — %(message)s', datefmt='[%X]', handlers=[RichHandler(rich_tracebacks=True, show_path=False)])
log = logging.getLogger('smoke')
log.debug('debug message')
log.info('info message')
log.warning('warning message')
log.error('error message')
try:
    raise ValueError('test exception')
except Exception:
    log.exception('exception with traceback')
"
```

Expected: five lines each with a colored level badge (`DEBUG` dim, `INFO` green, `WARNING` yellow, `ERROR`/`CRITICAL` red) and a rich-formatted traceback for the exception.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt main.py
git commit -m "feat: add colored logging via rich"
```
