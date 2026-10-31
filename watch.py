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

import warnings

import requests
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

# --- настройки --------------------------------------------------------------

STATE_FILE = Path("state/seen.json")
TIMEOUT = 25
UA = "Mozilla/5.0 (compatible; alta-watch/1.0; +https://github.com)"

# Что считаем относящимся к делу: Alta / Алта в любом регистре, кириллица и латиница.
KEYWORD_RE = re.compile(r"(?<![a-zа-яё])(alta|алта|альт[аыеуой]?)(?![a-zа-яё])", re.IGNORECASE)

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
NOTICE_KEYWORD_RE = re.compile(r"(alta|алта|исплат|isplat|осигуран|osiguran|депон|depon)", re.I)
# объявления с датой раньше этой не публикуем (процедура Alta началась 30.10.2026)
NOTICE_SINCE = datetime(2026, 10, 30, tzinfo=timezone.utc)
# предохранитель: если из одного источника внезапно «новых» больше, чем это, считаем сбоем и не постим
FLOOD_LIMIT = 20

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


RETRIES = 3
RETRY_PAUSE = 4


def fetch(url: str) -> requests.Response | None:
    """GET с повторами: aod.rs и nbs.rs бывают медленными."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=(15, TIMEOUT))
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < RETRIES:
                time.sleep(RETRY_PAUSE * attempt)
    log(f"WARN fetch failed after {RETRIES} попыток {url}: {last}")
    return None


def get(url: str) -> str | None:
    r = fetch(url)
    if r is None:
        return None
    r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
    return r.text


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
            m = re.search(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", ctx)
            date = f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else ""
            recent = False
            if m:
                try:
                    d = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
                    recent = d.timestamp() >= NOTICE_SINCE.timestamp()
                except ValueError:
                    recent = False
            # Старые объявления (про другие банки) не публикуем никогда.
            # Свежие — если упомянута Alta или речь о выплатах/вкладчиках.
            relevant = recent and bool(KEYWORD_RE.search(ctx) or NOTICE_KEYWORD_RE.search(ctx))
            items.append({
                "url": u,
                "title": ctx[:200],
                "date": date,
                "excerpt": "",
                "relevant": relevant,
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


ALTA_RECHECK: set[str] = set()   # страницы банка, которые перепроверяем на правки
ALTA_RECHECK_MAX = 10


def collect_alta() -> list[str]:
    urls: list[str] = []
    for page in ALTA_PAGES:
        text = get(page)
        if not text:
            continue
        page_urls: list[str] = []
        for m in ALTA_LINK_RE.finditer(text):
            slug = m.group(1).lower()
            if slug in ALTA_SKIP_SLUGS or slug.startswith("wp-"):
                continue
            u = norm_url(f"https://altabanka.rs/{slug}/") + "/"
            if u not in urls:
                urls.append(u)
                page_urls.append(u)
        # правки отслеживаем только у свежих заявлений (раздел «Саопштења»),
        # иначе каждый запуск перечитывал бы весь сайт
        if "saopstenje" in page:
            ALTA_RECHECK.update(page_urls[:ALTA_RECHECK_MAX])
    log(f"altabanka.rs: {len(urls)} ссылок, перепроверяем {len(ALTA_RECHECK)}")
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
        "full_text": full[:LLM_MAX_CHARS],
    }


# --- нейросеть: оценка важности и русская выжимка -------------------------------
# Включается, если задан ANTHROPIC_API_KEY. Без ключа работает словарный фильтр.

LLM_MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5")
LLM_MAX_CHARS = 7000              # сколько текста отдаём модели
LLM_TRANSLATE_OFFICIAL_UNDER = 1800  # короткие официальные сообщения переводим целиком
LLM_MIN_RELEVANCE_POST = 7        # отдельный пост
LLM_MIN_RELEVANCE_DIGEST = 5      # строка в подборке

LLM_SYSTEM = (
    "Ты редактор русскоязычного Telegram-канала для вкладчиков ALTA banka a.d. Beograd (Сербия). "
    "Контекст: 31.10.2026 Народный банк Сербии (NBS) отозвал у банка лицензию, начинается банкротство; "
    "управляющий — Агентство по страхованию вкладов (AOD). Вклады застрахованы до 50 000 евро на вкладчика; "
    "выплаты идут через банк-выплатчик, который AOD объявляет в течение 3 рабочих дней после решения "
    "Хозяйственного суда (Privredni sud u Beogradu) об открытии банкротства, выплаты начинаются в течение "
    "7 рабочих дней. Суммы сверх лимита заявляются как требования кредитора в процедуре банкротства.\n\n"
    "Читателям важно: решение суда и его дата; банк-выплатчик, даты, места, документы; особенности для "
    "нерезидентов и валютных вкладов; сроки подачи требований сверх 50 000 евро; судьба платежей в пути, "
    "карт, кредитов, зарплат и коммунальных платежей; новости об активах и владельце, влияющие на возврат "
    "сумм сверх лимита; заявления регуляторов о других банках.\n\n"
    "Оцени материал и ответь СТРОГО одним JSON-объектом без пояснений:\n"
    "{\n"
    '  "relevance": 0-10 (10 = прямо меняет, что делать вкладчику; 0 = не о процедуре; биографии владельца, '
    'общие рассуждения, повторы давно известного — не выше 4),\n'
    '  "title_ru": "короткий заголовок по-русски",\n'
    '  "summary_ru": ["3-6 тезисов по-русски, только факты из материала, цифры и даты сохранять"],\n'
    '  "translation_ru": "полный точный перевод на русский" — ТОЛЬКО если в задании сказано перевести, иначе null,\n'
    '  "action": "что сделать вкладчику, одна фраза" или null, если действий не требуется,\n'
    '  "unverified": true, если материал опирается на слухи, анонимные источники или заявления политиков '
    'без подтверждения регулятора,\n'
    '  "tags": ["до 4 тегов из списка: выплаты, суд, требования, регулятор, клиенты, активы, банки, сроки"]\n'
    "}\n"
    "Не додумывай фактов, которых нет в тексте. Не давай юридических советов. Если материал на русском, "
    "всё равно заполни все поля. Поле action заполняй ТОЛЬКО если материал прямо адресован вкладчикам или "
    "клиентам ALTA banka и содержит конкретный порядок действий; для документов о других банках и общих "
    "новостей — null. Пиши только кириллицей и латиницей, никаких иероглифов и других письменностей."
)

CJK_RE = re.compile(r"[⺀-鿿가-힯豈-﫿＀-￯]+")


def clean_ru(s: str) -> str:
    """Убирает случайные иероглифы, которые иногда вставляет модель."""
    return re.sub(r"\s{2,}", " ", CJK_RE.sub("", s)).strip()


def llm_analyze(item: dict, translate: bool = False) -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    text = item.get("full_text") or item.get("excerpt") or item.get("title") or ""
    if not text.strip():
        return None
    src = source_label(item)
    task = (
        f"Источник: {src}\nЗаголовок: {item.get('title', '')}\nДата: {item.get('date', '')}\n"
        f"Ссылка: {item.get('url', '')}\n\n"
        + ("ЗАДАНИЕ: это короткое официальное сообщение — сделай полный точный перевод на русский "
           "в поле translation_ru и, отдельно, тезисы.\n\n" if translate else
           "ЗАДАНИЕ: сделай тезисную выжимку (summary_ru); translation_ru = null.\n\n")
        + "ТЕКСТ:\n" + text[:LLM_MAX_CHARS]
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": LLM_MODEL,
                "max_tokens": 1500,
                "system": LLM_SYSTEM,
                "messages": [{"role": "user", "content": task}],
            },
            timeout=60,
        )
        if r.status_code != 200:
            log(f"WARN llm {r.status_code}: {r.text[:200]}")
            return None
        out = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            log("WARN llm: нет JSON в ответе")
            return None
        data = json.loads(m.group(0))
        data["relevance"] = int(data.get("relevance", 0) or 0)
        data["summary_ru"] = [clean_ru(str(s)) for s in (data.get("summary_ru") or []) if str(s).strip()][:6]
        data["tags"] = [str(t).strip().lstrip("#") for t in (data.get("tags") or [])][:4]
        for k in ("title_ru", "translation_ru", "action"):
            if data.get(k):
                data[k] = clean_ru(str(data[k]))
        # «Что делать» — только для действительно важного; иначе модель выдумывает советы
        if data["relevance"] < LLM_MIN_RELEVANCE_POST:
            data["action"] = None
        return data
    except Exception as e:  # noqa: BLE001
        log(f"WARN llm failed: {e}")
        return None


def document_text(url: str, max_chars: int = LLM_MAX_CHARS) -> str | None:
    """Текст из PDF или DOCX по ссылке (объявления AOD).
    None — не скачался (повторим позже); "" — скачался, но текста нет (скан)."""
    r = fetch(url)
    if r is None:
        return None
    data = r.content
    text = ""
    try:
        if url.lower().endswith(".pdf") or data[:5] == b"%PDF-":
            from io import BytesIO
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data))
            parts = []
            for page in reader.pages[:6]:
                parts.append(page.extract_text() or "")
                if sum(len(p) for p in parts) > max_chars:
                    break
            text = "\n".join(parts)
        elif url.lower().endswith((".docx", ".doc")) or data[:2] == b"PK":
            import zipfile
            from io import BytesIO
            with zipfile.ZipFile(BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    except Exception as e:  # noqa: BLE001
        log(f"WARN текст из документа не извлёкся {url}: {e}")
        return ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text[:max_chars]


def enrich(item: dict) -> dict:
    """Дополняет элемент русской выжимкой и оценкой важности, если включена нейросеть."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return item
    official = item.get("kind") in ("news", "alta_site", "aod_proceedings", "aod_notice")
    if item.get("kind") == "aod_notice" and not item.get("full_text"):
        text = document_text(item["url"])
        if text is None:
            item["defer"] = True  # не скачался — отложить до следующего прогона
            return item
        item["full_text"] = text
        if not text:
            log(f"оглас без текстового слоя (скан?), публикуем как есть: {item['url']}")
            return item
    if "full_text" not in item and item.get("kind") == "press":
        art = parse_article(item["url"])
        if art:
            item["full_text"] = art.get("full_text", "")
            item.setdefault("date", art.get("date", ""))
    translate = official and len(item.get("full_text", "")) <= LLM_TRANSLATE_OFFICIAL_UNDER
    data = llm_analyze(item, translate=translate)
    if data:
        item["llm"] = data
        if data.get("tags"):
            item["tags"] = data["tags"]
    return item


# --- тематический фильтр ------------------------------------------------------
# Публикация считается относящейся к делу, если упоминает Alta И хотя бы одну
# тему процесса. Темы становятся тегами в посте.

TOPICS: list[tuple[str, re.Pattern]] = [
    ("выплаты", re.compile(
        r"isplat|исплат|выплат|payout|osiguran|осигуран|застрахован|50[.\s]?000|deponent|депонент|"
        r"вкладчик|depozit|депозит|вклад|štedn|штедн|štediš|штедиш", re.I)),
    ("суд", re.compile(
        r"stečaj|stecaj|стечај|банкрот|bankrot|privredni sud|привредни суд|решењ|rešenj|решени[ея] суда|"
        r"ročišt|рочишт|upravnik|управник|управляющ|likvidac|ликвидац", re.I)),
    ("требования", re.compile(
        r"potraživanj|potrazivanj|потраживањ|требован|poverila|поверила|poverioc|повериоц|кредитор|"
        r"prijav[aeu]|пријав|стечајна маса|stečajn[aeu] mas", re.I)),
    ("регулятор", re.compile(
        r"\bnbs\b|\bнбс\b|narodn[ae] bank|народн[ае] банк|народн\w* банк|\baod\b|\bаод\b|agencij\w* za osiguranje|"
        r"агенциј\w* за осигурање|агентств\w* по страхован|dozvol\w* za rad|дозвол\w* за рад|лиценз|licenc", re.I)),
    ("клиенты", re.compile(
        r"kredit|кредит|platni promet|платни промет|платеж|kartic|картиц|банковск\w* карт|račun|рачун|"
        r"\bсч[её]т|trajn\w* nalog|zarad[ae]|зарад[ае]|зарплат|\beps\b|\bепс\b|komunal|комунал|alta ?pay|"
        r"klijent|клијент|клиент|ekspozitur|експозитур|отделени|aplikacij|апликациј|приложени", re.I)),
    ("активы", re.compile(
        r"imovin|имовин|актив[ыа]|istrag|истраг|расследован|tužila|тужила|прокурат|blokad|блокад|"
        r"transfer|трансфер|вывод\w* (денег|средств|капитал)|iznos\w* novca|iznošenj|изношењ|skoplj|скопљ|"
        r"moskv|москв|milion|милион|миллион", re.I)),
    ("банки", re.compile(
        r"udruženj\w* banaka|удружењ\w* банака|ostal\w* bana?k|друг\w* банк|bankarsk\w* sistem|банкарск\w* систем|"
        r"банковск\w* систем|stabilnost|стабилност|стабильност|poštansk\w* štedionic|поштанск\w* штедионic|"
        r"почтов\w* сберкасс|banka isplatila|банка исплатила|банк-выплатчик", re.I)),
    # только имя владельца — само по себе не повод для публикации
    ("владелец", re.compile(r"macur|мацур|макур|akcionar|акционар|vlasnik|власник|владел", re.I)),
    ("сроки", re.compile(
        r"\brok\b|rokov|\bрок\b|роков|\bсрок|deadline|ponedelj|понедељ|понедельник|radnih dana|радних дана|"
        r"рабочих дн", re.I)),
]
BIO_RE = re.compile(r"ko je |ко је |biografij|биографиј|kako je nastal|како је настал|menjačnic|мењачниц|"
                    r"imperij|империј|od menjačnice|од мењачнице|kako je izgradio|како је изградио", re.I)


def classify(text: str) -> tuple[bool, list[str]]:
    """→ (относится к делу?, теги). Нужны упоминание Alta и хотя бы одна тема, кроме чисто биографических."""
    if not KEYWORD_RE.search(text):
        return False, []
    tags = [name for name, rx in TOPICS if rx.search(text)]
    substantive = [t for t in tags if t != "владелец"]
    if BIO_RE.search(text[:200]) and len(substantive) < 2:
        return False, tags
    return bool(substantive) or len(tags) >= 2, tags


def tags_line(tags: list[str]) -> str:
    return " ".join("#" + t for t in tags)


# --- пресса (RSS проверенных изданий) ---------------------------------------

PRESS_FEEDS = [
    ("N1", "https://n1info.rs/feed/"),
    ("Danas", "https://www.danas.rs/feed/"),
    ("Nova ekonomija", "https://novaekonomija.rs/feed"),
    ("Forbes Srbija", "https://forbes.n1info.rs/feed/"),
    ("Nova.rs", "https://nova.rs/feed/"),
    ("Beta", "https://beta.rs/rss"),
    ("BIZLife", "https://bizlife.rs/feed/"),
]
PRESS_MAX_AGE_H = 48        # старше — не публикуем, даже если видим впервые
PRESS_MAX_PER_POST = 10


def _rss_field(item_xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item_xml, re.S | re.I)
    if not m:
        return ""
    v = m.group(1).strip()
    v = re.sub(r"^<!\[CDATA\[(.*?)\]\]>$", r"\1", v, flags=re.S)
    v = BeautifulSoup(v, "html.parser").get_text(" ", strip=True)
    return html.unescape(v).strip()


def parse_pubdate(s: str) -> datetime | None:
    from email.utils import parsedate_to_datetime
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def collect_press() -> list[dict]:
    """Статьи проверенных изданий, в которых Alta упомянута в заголовке или лиде."""
    items: list[dict] = []
    cutoff = datetime.now(timezone.utc).timestamp() - PRESS_MAX_AGE_H * 3600
    for outlet, feed in PRESS_FEEDS:
        text = get(feed)
        if not text or "<item" not in text:
            log(f"пресса {outlet}: лента не прочиталась")
            continue
        n = 0
        for raw in re.findall(r"<item\b.*?</item>", text, re.S | re.I):
            title = _rss_field(raw, "title")
            link = _rss_field(raw, "link") or ""
            if not link:
                m = re.search(r"<link[^>]*/>\s*([^<\s]+)", raw)  # атомный вариант
                link = m.group(1) if m else ""
            desc = _rss_field(raw, "description")[:300]
            pub = parse_pubdate(_rss_field(raw, "pubDate"))
            if not (title and link.startswith("http")):
                continue
            ok, tags = classify(title + " " + desc)
            if not ok:
                continue
            if pub and pub.timestamp() < cutoff:
                continue
            items.append({
                "url": norm_url(link),
                "title": title,
                "outlet": outlet,
                "date": pub.astimezone().strftime("%d.%m.%Y %H:%M") if pub else "",
                "kind": "press",
                "relevant": True,
                "tags": tags,
            })
            n += 1
        log(f"пресса {outlet}: {n} про Alta")
    return items


def format_press_digest(items: list[dict]) -> str:
    blocks = ["<b>📰 Пресса об ALTA banka</b>"]
    for it in items[:PRESS_MAX_PER_POST]:
        head = f"<b>{html.escape(it['outlet'])}</b>" + (f" · {it['date']}" if it.get("date") else "")
        body = f"<a href=\"{html.escape(it['url'])}\">{html.escape(it['title'])}</a>"
        llm = it.get("llm") or {}
        if llm.get("summary_ru"):
            body += "\n" + html.escape(llm["summary_ru"][0])
        tags = f"<i>{tags_line(it['tags'])}</i>" if it.get("tags") else ""
        blocks.append("\n".join(x for x in (head, body, tags) if x))
    blocks.append("<i>Подборка публикаций СМИ по темам процедуры. Проверяйте по первоисточникам: "
                  "официальные сообщения AOD, NBS и банка публикуются здесь отдельно.</i>")
    return "\n\n".join(blocks)


# --- Telegram-каналы (публичное веб-превью t.me/s/…) --------------------------
# Только каналы: у групп и чатов превью сообщений не показывает.

TG_CHANNELS = ["novisadaily", "Serbia", "volna_srbjia", "novostiserbii"]
TG_MAX_AGE_H = 48
TG_SNIPPET = 220


def collect_telegram() -> list[dict]:
    items: list[dict] = []
    cutoff = datetime.now(timezone.utc).timestamp() - TG_MAX_AGE_H * 3600
    for ch in TG_CHANNELS:
        text = get(f"https://t.me/s/{ch}")
        if not text or "tgme_widget_message" not in text:
            log(f"telegram @{ch}: превью не прочиталось")
            continue
        soup = BeautifulSoup(text, "html.parser")
        n = 0
        for msg in soup.select("div.tgme_widget_message[data-post]"):
            post = msg.get("data-post", "")
            if any(it["url"].endswith("/" + post) for it in items):
                continue
            body_el = msg.select_one(".tgme_widget_message_text")
            if not body_el or "/" not in post:
                continue
            body = body_el.get_text(" ", strip=True)
            ok, tags = classify(body)
            if not ok:
                continue
            t_el = msg.select_one("time[datetime]")
            pub = None
            if t_el:
                try:
                    pub = datetime.fromisoformat(t_el["datetime"].replace("Z", "+00:00"))
                except ValueError:
                    pub = None
            if pub and pub.timestamp() < cutoff:
                continue
            snippet = body[:TG_SNIPPET].rsplit(" ", 1)[0] + ("…" if len(body) > TG_SNIPPET else "")
            items.append({
                "url": f"https://t.me/{post}",
                "title": snippet,
                "full_text": body[:LLM_MAX_CHARS],
                "outlet": "@" + post.split("/")[0],
                "date": pub.astimezone().strftime("%d.%m.%Y %H:%M") if pub else "",
                "kind": "telegram",
                "relevant": True,
                "tags": tags,
            })
            n += 1
        log(f"telegram @{ch}: {n} по теме")
    return items


def format_tg_digest(items: list[dict]) -> str:
    blocks = ["<b>💬 Telegram-каналы об ALTA (неофициально)</b>"]
    for it in items[:PRESS_MAX_PER_POST]:
        head = f"<b>{html.escape(it['outlet'])}</b>" + (f" · {it['date']}" if it.get("date") else "")
        llm = it.get("llm") or {}
        text = llm.get("title_ru") or it["title"]
        if llm.get("summary_ru"):
            text += "\n" + html.escape(llm["summary_ru"][0])
        else:
            text = html.escape(text)
        body = f"{text} <a href=\"{html.escape(it['url'])}\">→ открыть</a>"
        tags = f"<i>{tags_line(it['tags'][:4])}</i>" if it.get("tags") else ""
        blocks.append("\n".join(x for x in (head, body, tags) if x))
    blocks.append("<i>Сообщения каналов, не проверены. Решения принимайте по официальным источникам.</i>")
    return "\n\n".join(blocks)


# --- Telegram ---------------------------------------------------------------


def tg_send(token: str, chat_id: str, text: str, no_preview: bool | None = None) -> bool:
    if no_preview is None:
        # подборки и объявления-PDF — без карточки предпросмотра, она только занимает место
        no_preview = text.startswith(("<b>📰", "<b>💬")) or "AOD — объявление" in text[:200]
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": no_preview,
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
    if item.get("outlet") and item.get("kind") == "press":
        head = f"<b>📰 {html.escape(item['outlet'])}</b>"
    if item.get("date"):
        head += f" · {item['date']}"
    if updated:
        head += " · <i>обновлено</i>"
    parts = [head]
    llm = item.get("llm")
    if llm:
        # русская версия от нейросети
        if llm.get("action"):
            parts.insert(0, f"⚠️ <b>Что делать:</b> {html.escape(str(llm['action']))}")
        parts.append(f"<b>{html.escape(llm.get('title_ru') or item.get('title', ''))}</b>")
        if llm.get("translation_ru"):
            parts.append(html.escape(str(llm["translation_ru"]))[:3000])
        elif llm.get("summary_ru"):
            parts.append("\n".join("• " + html.escape(s) for s in llm["summary_ru"]))
        if llm.get("unverified"):
            parts.append("❗️ <i>Содержит неподтверждённые регулятором утверждения.</i>")
        if item.get("title") and item.get("kind") != "aod_proceedings":
            parts.append(f"<i>Оригинал: {html.escape(item['title'])}</i>")
    else:
        if item.get("title"):
            parts.append(f"<b>{html.escape(item['title'])}</b>")
        if item.get("excerpt"):
            parts.append(html.escape(item["excerpt"]))
    label = "Открыть документ" if item.get("kind") == "aod_notice" else "Открыть первоисточник"
    tail = f'<a href="{html.escape(item["url"])}">{label}</a>'
    if item.get("tags"):
        tail += "  " + tags_line(item["tags"])
    parts.append(tail)
    note = ("Автоматический пересказ. Не является консультацией, проверяйте по первоисточнику."
            if llm else "Автоматическая публикация. Не является консультацией.")
    parts.append(f"<i>{note}</i>")
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

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    posted = 0

    def deliver(item: dict, updated: bool = False) -> bool:
        if item.get("kind") in ("news", "alta_site", "aod_proceedings", "aod_notice", "press") and "llm" not in item:
            enrich(item)
        if item.get("defer"):
            log(f"отложено до следующего прогона (не скачалось): {item['url']}")
            return False
        text = format_post(item, updated)
        if dry:
            print("---- DRY RUN POST ----\n" + text + "\n----------------------")
            return True
        return tg_send(token, chat_id, text)

    # 0) Принудительная публикация конкретных ссылок (FORCE_URLS="url1,url2"),
    #    например чтобы задним числом выложить сообщения, вышедшие до старта бота.
    force = [x.strip() for x in os.environ.get("FORCE_URLS", "").split(",") if x.strip()]
    for u in force:
        u = norm_url(u) + ("/" if "altabanka.rs" in u else "")
        if re.search(r"\.(pdf|docx?)$", u, re.I):
            m = re.search(r"/(\d+)_\d+_oglas", u)
            item = {"url": u, "title": f"Объявление AOD №{m.group(1)}" if m else "Документ",
                    "date": "", "excerpt": "", "relevant": True, "kind": "aod_notice", "hash": ""}
        else:
            item = parse_article(u)
        if item is None:
            log(f"FORCE: не прочиталось {u}")
            continue
        if deliver(item):
            seen[u] = {"first_seen": now, "posted": True, "forced": True,
                       "title": item["title"][:120], "hash": item["hash"]}
            posted += 1
            time.sleep(1.5)
    if force:
        save_state(state)
        log(f"FORCE: опубликовано {posted}")
        return 0

    # Страницы, которые нужно открыть и разобрать
    page_urls = collect_aod() + collect_nbs() + collect_alta()
    # Готовые элементы (объявления AOD — файлы, открывать не надо)
    ready_items = collect_aod_notices()
    if not page_urls and not ready_items:
        log("Ни один источник не ответил, выходим без изменений")
        return 0

    if first_run:
        # Первый запуск: запоминаем всё, что уже есть, ничего не публикуем,
        # чтобы не завалить канал старыми новостями.
        for u in page_urls:
            if u not in seen:
                rec = {"first_seen": now, "posted": False, "seeded": True}
                if u in ALTA_RECHECK:
                    it = parse_article(u)
                    if it:
                        rec["hash"] = it["hash"]
                seen[u] = rec
        for it in ready_items:
            seen.setdefault(it["url"], {"first_seen": now, "posted": False, "seeded": True})
        for it in collect_press() + collect_telegram():
            seen.setdefault(it["url"], {"first_seen": now, "posted": False, "seeded": True, "kind": it["kind"]})
        check_aod_proceedings(state)  # только выставить флаг, если Alta уже там
        state["seeded"] = True
        save_state(state)
        if not dry:
            tg_send(token, chat_id,
                    "🤖 Бот запущен. Слежу за сообщениями об ALTA banka: официальные — aod.rs, nbs.rs "
                    "и сайт банка; пресса — N1, Danas, Nova ekonomija, Forbes Srbija, Nova.rs, Beta, BIZLife. "
                    "Новые публикации появятся здесь автоматически.")
        log("Первый запуск: состояние засеяно")
        return 0

    # 1) Сигнал о появлении Alta в списке процедур AOD
    pr = check_aod_proceedings(state)
    if pr and deliver(pr):
        posted += 1
        time.sleep(1.5)

    # Предохранитель от потопа: слишком много «новых» сразу = источник раньше не читался.
    def flood_guard(label: str, urls: list[str]) -> bool:
        fresh = [u for u in urls if u not in seen]
        if len(fresh) > FLOOD_LIMIT:
            log(f"WARN {label}: {len(fresh)} новых сразу — похоже на сбой чтения, запоминаю без публикации")
            for u in fresh:
                seen[u] = {"first_seen": now, "posted": False, "flood": True}
            return True
        return False

    flood_guard("AOD огласи", [it["url"] for it in ready_items])
    flood_guard("страницы", page_urls)

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
        if rec is not None and u not in ALTA_RECHECK:
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

    # 4) Пресса и Telegram: новые публикации за запуск — одной подборкой каждая.
    #    BACKFILL=1 → выложить и те, что бот уже видел, но не публиковал (после пересева).
    backfill = os.environ.get("BACKFILL", "").lower() in ("1", "true", "yes")

    def is_new(it: dict) -> bool:
        rec = seen.get(it["url"])
        if rec is None:
            return True
        return backfill and not rec.get("posted")

    llm_on = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    for collector, formatter, label in (
        (collect_press, format_press_digest, "пресса"),
        (collect_telegram, format_tg_digest, "telegram"),
    ):
        fresh = [it for it in collector() if is_new(it)]
        if fresh and llm_on:
            # нейросеть: важное — отдельным постом с выжимкой, среднее — в подборку, остальное мимо
            keep: list[dict] = []
            for it in fresh[:PRESS_MAX_PER_POST * 2]:
                enrich(it)
                rel = (it.get("llm") or {}).get("relevance", 0)
                if it.get("llm") is None:
                    keep.append(it)  # нейросеть не ответила — оставляем словарный результат
                elif rel >= LLM_MIN_RELEVANCE_POST and label == "пресса" and posted < MAX_NEW_PER_RUN:
                    if deliver(it):
                        seen[it["url"]] = {"first_seen": now, "posted": True, "kind": it["kind"],
                                           "title": it["title"][:120], "tags": it.get("tags", []), "relevance": rel}
                        posted += 1
                        time.sleep(1.5)
                elif rel >= LLM_MIN_RELEVANCE_DIGEST:
                    if it["llm"].get("title_ru"):
                        it["title"] = it["llm"]["title_ru"]
                    keep.append(it)
                else:
                    seen[it["url"]] = {"first_seen": now, "posted": False, "kind": it["kind"],
                                       "title": it["title"][:120], "relevance": rel}
                    log(f"skip ({label}, {rel}/10): {it['title'][:70]}")
            fresh = keep
        if not fresh:
            continue
        text = formatter(fresh)
        if dry:
            print("---- DRY RUN POST ----\n" + text + "\n----------------------")
            ok = True
        else:
            ok = tg_send(token, chat_id, text)
        if ok:
            for it in fresh[:PRESS_MAX_PER_POST]:
                seen[it["url"]] = {"first_seen": now, "posted": True, "kind": it["kind"],
                                   "title": it["title"][:120], "tags": it.get("tags", [])}
            posted += 1
            time.sleep(1.5)
        else:
            log(f"Подборка ({label}) не отправилась, повторим позже")

    save_state(state)
    log(f"Готово. Опубликовано: {posted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
