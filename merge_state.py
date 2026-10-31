#!/usr/bin/env python3
"""Сливает два state/seen.json: объединение записей. Используется workflow при гонке запусков.
Запуск: python merge_state.py ours.json theirs.json out.json"""
import json
import sys


def load(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"seeded": False, "seen": {}}


ours, theirs, out = sys.argv[1:4]
a, b = load(ours), load(theirs)
merged = {
    "seeded": bool(a.get("seeded") or b.get("seeded")),
    "seen": {**b.get("seen", {}), **a.get("seen", {})},
    "flags": {**b.get("flags", {}), **a.get("flags", {})},
}
# если одна сторона уже публиковала — это важнее
for k, v in b.get("seen", {}).items():
    if v.get("posted") and not merged["seen"].get(k, {}).get("posted"):
        merged["seen"][k] = v
with open(out, "w", encoding="utf-8") as f:
    json.dump(merged, f, ensure_ascii=False, indent=1, sort_keys=True)
print(f"merged: {len(merged['seen'])} записей")
