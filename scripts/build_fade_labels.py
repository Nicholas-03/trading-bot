"""The news pairs behind the liquid_fade book, for a dedicated Laya fine-tune (kaggle/laya-trading, fade_* experiments).

    .venv/bin/python scripts/build_fade_labels.py   # data/laya_daily_labels.jsonl -> data/laya_fade_labels.jsonl

Keeps tradable pairs whose news day moved >= 8% against SPY either way (previous close -> close; the live book trades
>= 10%, the wider band gives the model more examples) and that have a 5-day return after the close.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_DAY_MOVE = 0.08

n = 0
with open(ROOT / "data" / "laya_fade_labels.jsonl", "w") as out:
    for line in open(ROOT / "data" / "laya_daily_labels.jsonl"):
        r = json.loads(line)
        if (r.get("tradable") and r.get("day_excess") is not None and abs(r["day_excess"]) >= MIN_DAY_MOVE
                and r.get("d5_excess") is not None and abs(r["d5_excess"]) < 1):  # |5-day| >= 100%: split errors
            out.write(line)
            n += 1
print(f"{n} pairs -> data/laya_fade_labels.jsonl")
