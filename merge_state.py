#!/usr/bin/env python3
"""Сливает два state/seen.json при гонке запусков, сохраняя ВСЕ разделы.
Запуск: python merge_state.py ours.json theirs.json out.json

Правила: seen — объединение, запись со статусом sent побеждает; recent — объединение по (url, ts);
pending — объединение по url, исключая уже отправленные; flags — объединение, announced побеждает;
signals — объединение по (url, ts); health — наше."""
import json
import sys


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


ours_p, theirs_p, out_p = sys.argv[1:4]
a, b = load(ours_p), load(theirs_p)

seen = {**b.get("seen", {}), **a.get("seen", {})}
for k, v in b.get("seen", {}).items():
    if v.get("status") == "sent" and seen.get(k, {}).get("status") != "sent":
        seen[k] = v

sent_urls = {k for k, v in seen.items() if v.get("status") == "sent"}

recent = {(r.get("url"), round(r.get("ts", 0))): r for r in b.get("recent", []) + a.get("recent", [])}
recent_list = sorted(recent.values(), key=lambda r: r.get("ts", 0))[-40:]

pending = {}
for label in set(a.get("pending", {})) | set(b.get("pending", {})):
    merged = {}
    for p in b.get("pending", {}).get(label, []) + a.get("pending", {}).get(label, []):
        if p["url"] not in sent_urls:
            merged.setdefault(p["url"], p)
    pending[label] = sorted(merged.values(), key=lambda p: p.get("ts", 0))

flags = {**b.get("flags", {}), **a.get("flags", {})}
for k, v in b.get("flags", {}).items():
    if k.endswith("_announced") and v and not flags.get(k):
        flags[k] = v

signals = {(s.get("url"), s.get("ts")): s for s in b.get("signals", []) + a.get("signals", [])}
signals_list = sorted(signals.values(), key=lambda s: s.get("ts", ""))[-50:]

out = {
    "seeded": bool(a.get("seeded") or b.get("seeded")),
    "seen": seen, "recent": recent_list, "pending": pending, "flags": flags,
    "signals": signals_list, "health": a.get("health") or b.get("health") or {},
}
with open(out_p, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
print(f"merged: seen {len(seen)}, recent {len(recent_list)}, pending {sum(len(v) for v in pending.values())}")
