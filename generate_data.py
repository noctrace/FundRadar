"""
Fund-Radar 数据生成脚本
======================
由 GitHub Actions 每天调用，生成静态 JSON 数据文件。
也可本地运行：python generate_data.py

生成的 JSON 文件：
  data/funds.json       — 高收益基金
  data/loss_funds.json  — 亏损基金
  data/surge_funds.json — 当日飙升基金 (>6%)
  data/plunge_funds.json— 当日暴跌基金 (<-6%)
  data/meta.json        — 元数据
"""

import json
import math
import os
import sys
import io
from datetime import datetime
from pathlib import Path

# 修复 Windows 控制台编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from fund_data import get_page_data, get_daily_surge_data

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

# 固定阈值
Y1, M6, M3, M1 = 100, 60, 40, 25


def sanitize(obj):
    """递归清洗 NaN/Inf 值为 0，确保生成合法 JSON。"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
        return obj
    elif isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize(item) for item in obj]
    return obj


def main():
    print(f"{'='*60}")
    print(f"  Fund-Radar 静态数据生成")
    print(f"  阈值: y1≥{Y1}%  m6≥{M6}%  m3≥{M3}%  m1≥{M1}%")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 确保 data/ 目录存在
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── 高收益基金 ──
    data = get_page_data(Y1, M6, M3, M1)

    funds_path = DATA_DIR / "funds.json"
    with open(funds_path, "w", encoding="utf-8") as f:
        json.dump(sanitize(data["fund_data"]), f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"[写入] {funds_path} ({data['fund_count']} 条)")

    loss_path = DATA_DIR / "loss_funds.json"
    with open(loss_path, "w", encoding="utf-8") as f:
        json.dump(sanitize(data["loss_fund_data"]), f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"[写入] {loss_path} ({data['loss_fund_count']} 条)")

    # ── 每日涨跌 ──
    print(f"\n{'─'*40}")
    daily_data = get_daily_surge_data()

    surge_path = DATA_DIR / "surge_funds.json"
    with open(surge_path, "w", encoding="utf-8") as f:
        json.dump(sanitize(daily_data["surge_fund_data"]), f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"[写入] {surge_path} ({daily_data['surge_fund_count']} 条)")

    plunge_path = DATA_DIR / "plunge_funds.json"
    with open(plunge_path, "w", encoding="utf-8") as f:
        json.dump(sanitize(daily_data["plunge_fund_data"]), f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"[写入] {plunge_path} ({daily_data['plunge_fund_count']} 条)")

    # ── 元数据 ──
    meta_path = DATA_DIR / "meta.json"
    meta = {
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "thresholds": {"y1": Y1, "m6": M6, "m3": M3, "m1": M1},
        "fund_count": data["fund_count"],
        "loss_fund_count": data["loss_fund_count"],
        "surge_fund_count": daily_data["surge_fund_count"],
        "plunge_fund_count": daily_data["plunge_fund_count"],
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[写入] {meta_path}")

    print(f"\n{'='*60}")
    print(f"  生成完成！")
    print(f"  高收益基金: {data['fund_count']} 只")
    print(f"  亏损基金:   {data['loss_fund_count']} 只")
    print(f"  当日飙升:   {daily_data['surge_fund_count']} 只")
    print(f"  当日暴跌:   {daily_data['plunge_fund_count']} 只")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
