import collections
import os
import re
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: production_log_summary.py LOG_FILE [TAIL_LINES]")
    log_path = Path(sys.argv[1])
    tail_lines = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.getenv("TAIL_LINES", "160"))
    lines = log_path.read_text(errors="replace").splitlines() if log_path.exists() else []

    skip_counts = collections.Counter()
    llm_counts = collections.Counter()
    markers = collections.Counter()
    error_samples: list[str] = []

    for line in lines:
        if "News received:" in line:
            markers["news_received"] += 1
        if "LLM decision" in line:
            markers["llm_decision"] += 1
            m = re.search(r"LLM decision \[[^\]]+\]:\s+(\w+)", line)
            if m:
                llm_counts[m.group(1)] += 1
        if "BUY TRIGGERED" in line:
            markers["buy_triggered"] += 1
        if "BUY filled" in line:
            markers["buy_filled"] += 1
        if "SHORT TRIGGERED" in line:
            markers["short_triggered"] += 1
        if "SHORT filled" in line:
            markers["short_filled"] += 1
        if "ORDER skipped" in line:
            markers["order_skipped_notification"] += 1
        if "ERROR" in line or "Traceback" in line:
            markers["error_lines"] += 1
            if len(error_samples) < 20:
                error_samples.append(line)
        m = re.search(r"SKIP \[([^\]]+)\]", line)
        if m:
            skip_counts[m.group(1)] += 1

    print("markers", dict(markers))
    print("llm_actions", dict(llm_counts))
    print("skip_counts", dict(skip_counts.most_common(40)))
    print("error_samples")
    for line in error_samples:
        print(line)

    interesting = [
        line for line in lines
        if any(token in line for token in (
            "News received:",
            "SKIP [",
            "LLM decision",
            "BUY TRIGGERED",
            "BUY PRE",
            "BUY filled",
            "SHORT TRIGGERED",
            "SHORT PRE",
            "SHORT filled",
            "ORDER skipped",
            "ERROR",
            "Traceback",
            "Bot starting",
        ))
    ]
    print("recent_matching_logs")
    for line in interesting[-tail_lines:]:
        print(line)


if __name__ == "__main__":
    main()
