#!/usr/bin/env bash
# Fine-tune Laya on a free Kaggle GPU (T4). Needs the Kaggle CLI, logged in. Everything is created private.
#
#   kaggle/laya-trading/run.sh upload     # 1. upload code + labels + experiments.txt as a private dataset (new version if it exists)
#   kaggle/laya-trading/run.sh start      # 2. push the notebook and start the GPU run
#   kaggle/laya-trading/run.sh status     #    check progress (RUNNING / COMPLETE / ERROR)
#   kaggle/laya-trading/run.sh logs       #    show the run log
#   kaggle/laya-trading/run.sh results    #    download the run's logs and test predictions to kaggle/laya-trading/results/
#   kaggle/laya-trading/run.sh download NAME  # 3. fetch experiment NAME's model into models/laya-trading/
set -euo pipefail
cd "$(dirname "$0")/../.."
KERNEL=nicholasb03/trading-bot-laya-finetune
DATASET=nicholasb03/trading-bot-laya-finetune-data
HERE=kaggle/laya-trading
STAGE=$HERE/.dataset   # gitignored staging folder

case "${1:-}" in
  upload)
    rm -rf "$STAGE" && mkdir -p "$STAGE"
    cp config.py advisor/laya_advisor.py scripts/finetune_laya.py scripts/predict_laya.py "$STAGE/"
    cp "$HERE/experiments.txt" kaggle/laya-eval/eval.txt kaggle/laya-premarket/experiments_premarket.txt "$STAGE/"
    cp data/laya_alpaca_labels_v2.jsonl data/laya_premarket.jsonl data/laya_labels_2021_2026.jsonl "$STAGE/"
    printf '{"title": "Trading Bot Laya Finetune Data", "id": "%s", "licenses": [{"name": "other"}]}\n' "$DATASET" \
      > "$STAGE/dataset-metadata.json"
    if kaggle datasets status "$DATASET" >/dev/null 2>&1; then
      kaggle datasets version -p "$STAGE" -m "update $(date +%F-%H%M)"
    else
      kaggle datasets create -p "$STAGE"            # private by default
    fi ;;
  start)    kaggle kernels push -p "$HERE" ;;
  eval)     kaggle kernels push -p kaggle/laya-eval ;;          # score models on held-out data (kaggle/laya-eval/eval.txt)
  premarket) kaggle kernels push -p kaggle/laya-premarket ;;  # pre-market news traded at the open (experiments_premarket.txt)
  eval-results)
    mkdir -p "$HERE/results/eval" && kaggle kernels output nicholasb03/trading-bot-laya-eval -p "$HERE/results/eval" ;;
  status)   kaggle kernels status "$KERNEL" ;;
  logs)     kaggle kernels logs "$KERNEL" ;;
  results)
    mkdir -p "$HERE/results" && rm -rf "$HERE/results/logs"   # keep results/eval (from eval-results)
    kaggle kernels output "$KERNEL" -p "$HERE/results" --file-pattern '.*(\.log|test_predictions\.jsonl|rl_agent_config\.json)$' ;;
  download)
    name=${2:?experiment name}
    tmp=$(mktemp -d)
    kaggle kernels output "$KERNEL" -p "$tmp" --file-pattern "^$name/.*"
    test -f "$tmp/$name/model.safetensors" || { echo "no model in the output: $tmp"; exit 1; }
    if [ -d models/laya-trading ]; then mv models/laya-trading "models/laya-trading.bak-$(date +%s)"; fi
    mkdir -p models && mv "$tmp/$name" models/laya-trading
    echo "model in models/laya-trading (previous one kept as models/laya-trading.bak-*)" ;;
  *) sed -n 2,9p "$0"; exit 1 ;;
esac
