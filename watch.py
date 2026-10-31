#!/usr/bin/env python3
"""
alta-watch: следит за официальными сообщениями об ALTA banka
на сайтах Агентства по страхованию вкладов (aod.rs) и Народного банка Сербии (nbs.rs)
и публикует новые в Telegram-канал. Только официальные источники.

Запускается GitHub Actions по расписанию. Состояние (что уже опубликовано)
хранится в state/seen.json и коммитится обратно в репозиторий.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- настройки --------------------------------------------------------------

STATE_FILE = Path("state/seen.json")
TIMEOUT = 25
UA = "Mozilla/5.0 (compatible; alta-watch/1.0; +https://github.com)"

# Что считаем относящимся к делу: Alta / Алта в любом регистре, кириллица и латиница.
KEYWORD_RE = re.compile(r"(?<![a-zа-яё])(alta|алта)(?![a-zа-яё])", re.IGNORECASE)

AOD_PAGES = [
    "https://www.aod.rs/",
    "https://www.aod.rs/clan/vest/sve_vesti",
]
AOD_LINK_RE = re.compile(r"https?://(?:www\.)?aod\.rs/clan/vest/vest_forma/\d+", re.I)

# Объявления AOD («Огласи») — это PDF/DOCX в листинге, открывать их не нужно:
# относимся к ним по тексту рядом со ссылкой.
AOD_NOTICE_PAGES = [
    "https://www.aod.rs/clan/oglas/akt_oglasi",
    "https://www.aod.rs/clan/oglasdok/arhiva",
]
AOD_NOTICE_FILE_RE = re.compile(r"/uploads/dokumenti/oglas/[^\s\"'<>#?]+\.(?:pdf|docx?)", re.I)
# Для объявлений фильтр шире: любое объявление про выплаты/вкладчиков/банкротство,
# а не только с именем банка, чтобы не пропустить «Оглас о исплати осигураних депозита».
NOTICE_KEYWORD_RE = re.compile(r"(alta|алта|исплат|isplat|осигуран|osiguran|депон|depon|стечај|stečaj|stecaj)", re.I)

# Страница AOD со списком банков в процедуре: сигналим один раз, когда там появится Alta.
AOD_PROCEEDINGS_PAGE = "https://www.aod.rs/clan/stecajlikv/postupci_utoku"

# Официальный сайт самого банка.
ALTA_PAGES = [
    "https://altabanka.rs/saopstenje/",
    "https://altabanka.rs/vesti/",
]
ALTA_LINK_RE = re.compile(r"https?://(?:www\.)?altabanka\.rs/([a-z0-9][a-z0-9-]{3,})/?(?=[\"'\s<>#?])", re.I)
ALTA_SKIP_SLUGS = {
    "saopstenje", "vesti", "kontakt", "o-nama", "stanovnistvo", "privreda", "kursna-lista",
    "e-banking", "m-banking", "karijera", "medija-centar", "dokumenti", "lokacije", "en",
    "wp-content", "wp-admin", "wp-json", "feed", "tag", "category", "author", "page",
}

NBS_RSS = (
    "https://www.nbs.rs/system/modules/yu.nbs.news/elements/responsive/"
    "RSSnews.xml?kategorija=158&lang=sr_RS&konverzija=no"
)
NBS_FALLBACK_PAGES = [
    "https://www.nbs.rs/sr_RS/drugi-nivo-navigacije/pres/arhiva-vesti/",
    "https://www.nbs.rs/sr_RS/drugi-nivo-navigacije/pres/",
    "https://www.nbs.rs/sr_RS/indeks/",
]
NBS_LINK_RE = re.compile(r"https?://(?:www\.)?nbs\.rs/[a-z_A-Z]+/scripts/showcontent/index\.html\?id=\d+", re.I)

MAX_EXCERPT = 600          # символов тела сообщения в посте
MAX_NEW_PER_RUN = 8        # защита от потопа, если что-то пошло не так

# ---------------------------------------------------------------------------


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def get(url: str) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
        return r.text
    except Exception as e:  # noqa: BLE001
        log(f"WARN fetch failed {url}: {e}")
        return None


def norm_url(u: str) -> str:
    u = html.unescape(u.strip())
    u = re.sub(r"^http://", "https://", u)
    u = re.sub(r"^https://aod\.rs", "https://www.aod.rs", u)
    u = re.sub(r"^https://nbs\.rs", "https://www.nbs.rs", u)
    return u.rstrip("/")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"seeded": False, "seen": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")


# --- сбор ссылок ------------------------------------------------------------


def collect_aod() -> list[str]:
    urls: list[str] = []
    for page in AOD_PAGES:
        text = get(page)
        if not text:
            continue
        for m in AOD_LINK_RE.findall(text):
            u = norm_url(m)
            if u not in urls:
                urls.append(u)
        # относительные ссылки
        for m in re.findall(r"href=[\"'](/clan/vest/vest_forma/\d+)", text, re.I):
            u = norm_url("https://www.aod.rs" + m)
            if u not in urls:
                urls.append(u)
    log(f"AOD вести: {len(urls)} ссылок")
    return urls


def collect_aod_notices() -> list[dict]:
    """Объявления AOD: возвращает готовые элементы (url, title, date, relevant), без открытия файлов."""
    items: list[dict] = []
    seen_local: set[str] = set()
    for page in AOD_NOTICE_PAGES:
        text = get(page)
        if not text:
            continue
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not AOD_NOTICE_FILE_RE.search(href):
                continue
            u = norm_url(href if href.startswith("http") else "https://www.aod.rs" + href)
            if u in seen_local:
                continue
            seen_local.add(u)
            # контекст: текст ссылки + ближайший небольшой блок-родитель (до даты),
            # но не весь список целиком
            ctx = a.get_text(" ", strip=True)
            parent = a
            for _ in range(3):
                if parent.parent is None:
                    break
                parent = parent.parent
                t = re.sub(r"\s+", " ", parent.get_text(" ", strip=True))
                if len(t) > 300:
                    break
                ctx = t
                if re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", t):
                    break
            ctx = re.sub(r"\s+", " ", ctx)[:300]
            m = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", ctx)
            items.append({
                "url": u,
                "title": ctx[:200],
                "date": m.group(1) if m else "",
                "excerpt": "",
                "relevant": bool(NOTICE_KEYWORD_RE.search(ctx)),
                "kind": "aod_notice",
            })
    log(f"AOD огласи: {len(items)} файлов")
    return items


def check_aod_proceedings(state: dict) -> dict | None:
    """Один раз сообщить, когда Alta появится в списке процедур AOD."""
    text = get(AOD_PROCEEDINGS_PAGE)
    if not text:
        return None
    plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    present = bool(KEYWORD_RE.search(plain))
    flag = state.setdefault("flags", {})
    if present and not flag.get("alta_in_proceedings"):
        flag["alta_in_proceedings"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {
            "url": AOD_PROCEEDINGS_PAGE,
            "title": "ALTA banka появилась в списке процедур банкротства/ликвидации на сайте AOD",
            "date": "",
            "excerpt": "На странице «Поступци у току» Агентства появился раздел по ALTA banka. "
                       "Там обычно публикуются решения суда, объявления для вкладчиков и кредиторов, отчёты.",
            "relevant": True,
            "kind": "aod_proceedings",
        }
    return None


def collect_alta() -> list[str]:
    urls: list[str] = []
    for page in ALTA_PAGES:
        text = get(page)
        if not text:
            continue
        for m in ALTA_LINK_RE.finditer(text):
            slug = m.group(1).lower()
            if slug in ALTA_SKIP_SLUGS or slug.startswith("wp-"):
                continue
            u = norm_url(f"https://altabanka.rs/{slug}/") + "/"
            if u not in urls:
                urls.append(u)
    log(f"altabanka.rs: {len(urls)} ссылок")
    return urls


def collect_nbs() -> list[str]:
    urls: list[str] = []
    text = get(NBS_RSS)
    if text:
        for m in re.findall(r"<link>\s*(.*?)\s*</link>", text, re.S):
            m = html.unescape(m)
            if NBS_LINK_RE.match(m):
                u = norm_url(m)
                if u not in urls:
                    urls.append(u)
        log(f"NBS RSS: {len(urls)} ссылок")
    if not urls:
        for page in NBS_FALLBACK_PAGES:
            t = get(page)
            if not t:
                continue
            for m in NBS_LINK_RE.findall(t):
                u = norm_url(m)
                if u not in urls:
                    urls.append(u)
            for m in re.findall(r"href=[\"'](/[a-z_A-Z]+/scripts/showcontent/index\.html\?id=\d+)", t):
                u = norm_url("https://www.nbs.rs" + html.unescape(m))
                if u not in urls:
                    urls.append(u)
        log(f"NBS fallback: {len(urls)} ссылок")
    return urls


# --- разбор статьи ----------------------------------------------------------


def parse_article(url: str) -> dict | None:
    text = get(url)
    if not text:
        return None
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()

    title = ""
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"].strip()
    if not title:
        for h in ("h1", "h2", "h3"):
            el = soup.find(h)
            if el and el.get_text(strip=True):
                title = el.get_text(" ", strip=True)
                break
    if not title and soup.title:
        title = soup.title.get_text(strip=True)

    body_text = soup.get_text("\n", strip=True)
    body_text = re.sub(r"\n{2,}", "\n", body_text)

    date = ""
    pub = soup.find("meta", attrs={"property": "article:published_time"})
    if pub and pub.get("content"):
        date = pub["content"][:10]  # YYYY-MM-DD
        if re.match(r"\d{4}-\d{2}-\d{2}$", date):
            y, mo, d = date.split("-")
            date = f"{d}.{mo}.{y}"
    if not date:
        m = re.search(r"\b(\d{2})[./](\d{2})[./](\d{4})\.?\b", body_text)
        if m:
            date = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"

    # фрагмент: первые абзацы после заголовка
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    paras = [p for p in paras if len(p) > 40]
    full = " ".join(paras)
    if len(full) > MAX_EXCERPT:
        excerpt = full[:MAX_EXCERPT].rsplit(" ", 1)[0] + "…"
    else:
        excerpt = full

    is_alta_site = "altabanka.rs" in url
    return {
        "url": url,
        "title": title,
        "date": date,
        "excerpt": excerpt,
        # с сайта самого банка берём всё; у регуляторов — только упоминания Alta
        "relevant": is_alta_site or bool(KEYWORD_RE.search(title) or KEYWORD_RE.search(body_text)),
        "hash": hashlib.sha1((title + "\n" + full).encode("utf-8")).hexdigest()[:16],
        "kind": "alta_site" if is_alta_site else "news",
    }


# --- Telegram ---------------------------------------------------------------


def tg_send(token: str, chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log(f"WARN telegram {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log(f"WARN telegram failed: {e}")
        return False


def source_label(item: dict) -> str:
    u = item["url"]
    kind = item.get("kind", "")
    if kind == "aod_notice":
        return "📄 AOD — объявление (оглас)"
    if kind == "aod_proceedings":
        return "🏛 AOD — процедуры банкротства"
    if "aod.rs" in u:
        return "🏛 AOD (Агентство по страхованию вкладов)"
    if "nbs.rs" in u:
        return "🏛 NBS (Народный банк Сербии)"
    if "altabanka.rs" in u:
        return "🏦 ALTA banka — сайт банка"
    return "🏛 Официальный источник"


def format_post(item: dict, updated: bool = False) -> str:
    head = f"<b>{html.escape(source_label(item))}</b>"
    if item.get("date"):
        head += f" · {item['date']}"
    if updated:
        head += " · <i>обновлено</i>"
    parts = [head]
    if item.get("title"):
        parts.append(f"<b>{html.escape(item['title'])}</b>")
    if item.get("excerpt"):
        parts.append(html.escape(item["excerpt"]))
    label = "Открыть документ" if item.get("kind") == "aod_notice" else "Открыть первоисточник"
    parts.append(f'<a href="{html.escape(item["url"])}">{label}</a>')
    parts.append("<i>Автоматическая публикация официального сообщения. Не является консультацией.</i>")
    return "\n\n".join(parts)


# --- main -------------------------------------------------------------------


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    dry = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    if not dry and (not token or not chat_id):
        log("ERROR: нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return 2

    state = load_state()
    seen: dict = state.setdefault("seen", {})
    first_run = not state.get("seeded", False)

    # Страницы, которые нужно открыть и разобрать
    page_urls = collect_aod() + collect_nbs() + collect_alta()
    # Готовые элементы (объявления AOD — файлы, открывать не надо)
    ready_items = collect_aod_notices()
    if not page_urls and not ready_items:
        log("Ни один источник не ответил, выходим без изменений")
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    posted = 0

    def deliver(item: dict, updated: bool = False) -> bool:
        text = format_post(item, updated)
        if dry:
            print("---- DRY RUN POST ----\n" + text + "\n----------------------")
            return True
        return tg_send(token, chat_id, text)

    if first_run:
        # Первый запуск: запоминаем всё, что уже есть, ничего не публикуем,
        # чтобы не завалить канал старыми новостями.
        for u in page_urls:
            if u not in seen:
                rec = {"first_seen": now, "posted": False, "seeded": True}
                if "altabanka.rs" in u:
                    it = parse_article(u)
                    if it:
                        rec["hash"] = it["hash"]
                seen[u] = rec
        for it in ready_items:
            seen.setdefault(it["url"], {"first_seen": now, "posted": False, "seeded": True})
        check_aod_proceedings(state)  # только выставить флаг, если Alta уже там
        state["seeded"] = True
        save_state(state)
        if not dry:
            tg_send(token, chat_id,
                    "🤖 Бот запущен. Слежу за официальными сообщениями об ALTA banka: "
                    "aod.rs (новости и объявления), nbs.rs и сайт банка altabanka.rs. "
                    "Новые публикации появятся здесь автоматически.")
        log("Первый запуск: состояние засеяно")
        return 0

    # 1) Сигнал о появлении Alta в списке процедур AOD
    pr = check_aod_proceedings(state)
    if pr and deliver(pr):
        posted += 1
        time.sleep(1.5)

    # 2) Объявления AOD
    for it in ready_items:
        if posted >= MAX_NEW_PER_RUN:
            break
        if it["url"] in seen:
            continue
        if not it["relevant"]:
            seen[it["url"]] = {"first_seen": now, "posted": False, "relevant": False, "title": it["title"][:120]}
            continue
        if deliver(it):
            seen[it["url"]] = {"first_seen": now, "posted": True, "title": it["title"][:120]}
            posted += 1
            time.sleep(1.5)

    # 3) Новости AOD, NBS и сайта банка
    for u in page_urls:
        if posted >= MAX_NEW_PER_RUN:
            log("Достигнут лимит постов за запуск, остальное — в следующий раз")
            break
        rec = seen.get(u)
        is_alta_site = "altabanka.rs" in u
        if rec is not None and not is_alta_site:
            continue
        item = parse_article(u)
        if item is None:
            continue  # не прочиталось — попробуем в следующий раз
        if rec is not None:
            # сайт банка: тот же адрес, но изменился текст → публикуем как обновление
            if rec.get("hash") and rec["hash"] != item["hash"]:
                if deliver(item, updated=True):
                    rec.update({"hash": item["hash"], "updated": now})
                    posted += 1
                    time.sleep(1.5)
            elif not rec.get("hash"):
                rec["hash"] = item["hash"]
            continue
        if not item["relevant"]:
            seen[u] = {"first_seen": now, "posted": False, "relevant": False, "title": item["title"][:120]}
            log(f"skip (не про Alta): {item['title'][:80]}")
            continue
        if deliver(item):
            seen[u] = {"first_seen": now, "posted": True, "title": item["title"][:120], "hash": item["hash"]}
            posted += 1
            time.sleep(1.5)
        else:
            log(f"Не отправилось, повторим позже: {u}")

    save_state(state)
    log(f"Готово. Опубликовано: {posted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
