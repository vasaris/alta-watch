"""Юнит-тесты alta-watch: без сети, без ключей, без отправок.
Запуск: python -m unittest discover -s tests -v"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DRY_RUN", "1")  # модуль читает DRY_RUN при импорте

import watch  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


class FakeResp:
    def __init__(self, status=200, text="", content=b"", js=None):
        self.status_code, self.text, self.content, self._js = status, text, content, js

    def json(self):
        return self._js if self._js is not None else json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise watch.requests.HTTPError(str(self.status_code))


def llm_body(**over):
    data = {"subject": "alta_beograd", "relevance": 8, "novelty": 8, "title_ru": "Заголовок",
            "summary_ru": ["Тезис один", "Тезис два"], "new_facts": ["Факт"], "translation_ru": None,
            "action": None, "unverified": False, "tags": ["выплаты"], "facts": []}
    data.update(over)
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


class TestUrlsAndHosts(unittest.TestCase):
    def test_norm_url(self):
        self.assertEqual(watch.norm_url("http://aod.rs/clan/vest/vest_forma/137/#x"),
                         "https://www.aod.rs/clan/vest/vest_forma/137")
        self.assertEqual(watch.norm_url("https://n1info.rs/a/?utm_source=tg&id=5"), "https://n1info.rs/a/?id=5")
        self.assertEqual(watch.norm_url("https://www.altabanka.rs/saopstenje/"), "https://altabanka.rs/saopstenje")

    def test_hosts(self):
        self.assertTrue(watch.is_aod("https://www.aod.rs/x"))
        self.assertFalse(watch.is_aod("https://evil.example/www.aod.rs/x"))
        self.assertFalse(watch.is_aod("https://aod.rs.evil.example/"))
        self.assertTrue(watch.is_alta_site("https://altabanka.rs/vesti/"))
        self.assertIn("n1info.rs", watch.press_hosts())


class TestAodNotices(unittest.TestCase):
    def test_dates_inherit_and_attachments(self):
        html_text = (FIX / "aod_akt_oglasi.html").read_text("utf-8")
        with mock.patch.object(watch, "get", side_effect=lambda u: html_text if u.endswith("akt_oglasi") else None):
            items, health = watch.collect_aod_notices()
        self.assertEqual(health["pages_ok"], 1)
        self.assertEqual(health["files"], 4)
        by_url = {it["url"].rsplit("/", 1)[-1]: it for it in items}
        self.assertEqual(by_url["101_1_oglas.pdf"]["date"], "31.10.2026")
        self.assertTrue(by_url["101_1_oglas.pdf"]["relevant"])
        # приложение наследует дату и тему объявления
        self.assertEqual(by_url["101_2_oglas.pdf"]["date"], "31.10.2026")
        self.assertIn("Прилог 1", by_url["101_2_oglas.pdf"]["title"])
        self.assertTrue(by_url["101_2_oglas.pdf"]["parse_ok"])
        # старое объявление про другой банк — не свежее
        self.assertEqual(by_url["77_1_oglas.docx"]["date"], "12.03.2024")
        self.assertFalse(by_url["77_1_oglas.docx"]["recent"])
        # файл без даты — ошибка разбора, не «старое»
        self.assertFalse(by_url["99_1_oglas.pdf"]["parse_ok"])
        self.assertEqual(health["dated"], 3)


class TestNearDup(unittest.TestCase):
    def test_same_wire_headline(self):
        others = [("Direktor AOD: isplata depozita Alta banke počinje u ponedeljak", "https://n1info.rs/a")]
        self.assertEqual(watch.near_dup("Direktor AOD: isplata depozita Alta banke počinje u ponedeljak", others),
                         "https://n1info.rs/a")
        # диакритика и регистр не мешают
        self.assertEqual(watch.near_dup("DIREKTOR AOD: ISPLATA DEPOZITA ALTA BANKE POCINJE U PONEDELJAK", others),
                         "https://n1info.rs/a")

    def test_negation_and_numbers_break_dup(self):
        others = [("Isplata depozita Alta banke počinje u ponedeljak 3. novembra", "u1")]
        self.assertIsNone(watch.near_dup("Isplata depozita Alta banke ne počinje u ponedeljak 3. novembra", others))
        self.assertIsNone(watch.near_dup("Isplata depozita Alta banke počinje u ponedeljak 10. novembra", others))

    def test_different_headlines(self):
        others = [("Tužilaštvo traži podatke o transferima Alta banke", "u1")]
        self.assertIsNone(watch.near_dup("Vučić: štediše Alta banke dobiće novac", others))
        self.assertIsNone(watch.near_dup("Alta", others))  # слишком коротко для вывода


class TestRetryLogic(unittest.TestCase):
    def setUp(self):
        self.state = watch.empty_state()

    def test_systemic_reasons_do_not_count_attempts(self):
        it = {"url": "https://n1info.rs/a", "kind": "press", "title": "t", "title_src": "t", "outlet": "N1", "ts": time.time()}
        for _ in range(20):
            watch.bump_retry(self.state, it["url"], "llm_down", item=it)
            watch.bump_retry(self.state, it["url"], "llm_budget", item=it)
        rec = self.state["seen"][it["url"]]
        self.assertEqual(rec["status"], "retry")
        self.assertEqual(rec["attempts"], 0)
        self.assertIn(it["url"], self.state["retry_items"])  # метаданные сохранены для повтора без ленты

    def test_real_failures_quarantine_after_limit(self):
        u = "https://n1info.rs/b"
        for _ in range(watch.RETRY_MAX_ATTEMPTS):
            status = watch.bump_retry(self.state, u, "download")
        self.assertEqual(status, "quarantine")
        self.assertTrue(self.state["seen"][u]["reason"].startswith("retry_exhausted"))

    def test_mark_terminal_clears_retry_item(self):
        it = {"url": "https://n1info.rs/c", "kind": "press", "title": "t", "ts": time.time()}
        watch.bump_retry(self.state, it["url"], "llm", item=it)
        self.assertIn(it["url"], self.state["retry_items"])
        watch.mark(self.state, it["url"], "sent", message_id=1)
        self.assertNotIn(it["url"], self.state["retry_items"])

    def test_fresh_when_seen(self):
        three_h = 3 * 3600
        old_ts = time.time() - 5 * 3600
        self.assertFalse(watch.was_fresh_when_seen(None, old_ts, three_h))          # впервые увидели старое
        rec = {"first_seen": watch.datetime.fromtimestamp(old_ts + 600, watch.timezone.utc).isoformat()}
        self.assertTrue(watch.was_fresh_when_seen(rec, old_ts, three_h))           # увидели свежим, повтор позже
        self.assertFalse(watch.was_fresh_when_seen(rec, None, three_h))

    def test_retry_items_merge_and_expire(self):
        fresh = {"url": "https://n1info.rs/d", "kind": "press", "title": "t", "ts": time.time() - 3600}
        stale = {"url": "https://n1info.rs/e", "kind": "press", "title": "t", "ts": time.time() - 80 * 3600}
        for it in (fresh, stale):
            watch.bump_retry(self.state, it["url"], "llm", item=it)
        out = watch.retry_items_for(self.state, "press", [])
        self.assertEqual([it["url"] for it in out], [fresh["url"]])
        self.assertEqual(self.state["seen"][stale["url"]]["status"], "rejected")
        self.assertNotIn(stale["url"], self.state["retry_items"])


class TestLlm(unittest.TestCase):
    def setUp(self):
        watch.LLM_DOWN = None
        watch.LLM_NET_FAILS = 0
        watch.LLM_OK = False
        watch.LLM_CALLS = 0
        os.environ["ANTHROPIC_API_KEY"] = "test"

    def tearDown(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        watch.LLM_DOWN = None

    def test_billing_error_marks_down_and_skips_further_calls(self):
        resp = FakeResp(400, json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                                     "message": "Your credit balance is too low to access the Anthropic API."}}))
        item = {"url": "u", "kind": "press", "title": "t", "full_text": "Alta banka text"}
        with mock.patch.object(watch.requests, "post", return_value=resp) as post:
            self.assertIsNone(watch.llm_analyze(item))
            self.assertTrue(watch.LLM_DOWN and watch.LLM_DOWN.startswith("billing"))
            self.assertIsNone(watch.llm_analyze(item))
            self.assertEqual(post.call_count, 1)   # второй раз в API не ходим
        out = watch.enrich(dict(item))
        self.assertEqual(out["defer"], "llm_down")

    def test_other_400_is_not_systemic(self):
        resp = FakeResp(400, json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long"}}))
        with mock.patch.object(watch.requests, "post", return_value=resp):
            self.assertIsNone(watch.llm_analyze({"url": "u", "kind": "press", "title": "t", "full_text": "x"}))
        self.assertIsNone(watch.LLM_DOWN)

    def test_classify(self):
        self.assertEqual(watch.classify_llm_error(429, ""), "rate_limit")
        self.assertEqual(watch.classify_llm_error(529, ""), "server")
        self.assertEqual(watch.classify_llm_error(401, ""), "auth")
        self.assertIsNone(watch.classify_llm_error(400, "invalid json"))

    def test_schema_and_facts(self):
        body = llm_body(facts=[{"key": "payout_bank", "value": "Banka X"}, {"key": "bogus", "value": "y"}, "junk"])
        with mock.patch.object(watch.requests, "post", return_value=FakeResp(200, js=body)):
            data = watch.llm_analyze({"url": "https://www.aod.rs/clan/vest/vest_forma/1", "kind": "news",
                                      "title": "t", "full_text": "Alta banka"})
        self.assertEqual(data["facts"], [{"key": "payout_bank", "value": "Banka X"}])
        self.assertTrue(watch.LLM_OK)
        # пресса фактов не даёт даже если модель прислала
        with mock.patch.object(watch.requests, "post", return_value=FakeResp(200, js=body)):
            data = watch.llm_analyze({"url": "https://n1info.rs/a", "kind": "press", "title": "t", "full_text": "Alta"})
        self.assertEqual(data["facts"], [])

    def test_invalid_schema_returns_none(self):
        body = llm_body(novelty="high")
        with mock.patch.object(watch.requests, "post", return_value=FakeResp(200, js=body)):
            self.assertIsNone(watch.llm_analyze({"url": "u", "kind": "press", "title": "t", "full_text": "Alta"}))

    def test_scan_pdf_sent_as_document(self):
        item = {"url": "https://www.aod.rs/uploads/dokumenti/oglas/1_1_oglas.pdf", "kind": "aod_notice",
                "title": "Oglas Alta", "full_text": "", "pdf": b"%PDF-1.4 fake"}
        with mock.patch.object(watch.requests, "post", return_value=FakeResp(200, js=llm_body())) as post:
            self.assertIsNotNone(watch.llm_analyze(item))
        content = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "document")
        self.assertEqual(content[0]["source"]["media_type"], "application/pdf")

    def test_update_facts_registry(self):
        state = watch.empty_state()
        item = {"url": "https://www.aod.rs/clan/vest/vest_forma/1", "kind": "news",
                "llm": {"facts": [{"key": "payout_bank", "value": "Banka X"}]}}
        self.assertEqual(watch.update_facts(state, item), ["payout_bank"])
        self.assertEqual(watch.update_facts(state, item), [])          # то же значение — без изменений
        item["llm"]["facts"][0]["value"] = "Banka Y"
        self.assertEqual(watch.update_facts(state, item), ["payout_bank"])
        self.assertEqual(state["facts"]["payout_bank"]["history"][0]["value"], "Banka X")
        self.assertIn("Banka Y", watch.known_context())
        press = {"url": "https://n1info.rs/a", "kind": "press", "llm": {"facts": [{"key": "other", "value": "z"}]}}
        self.assertEqual(watch.update_facts(state, press), [])


class TestDocuments(unittest.TestCase):
    def test_old_doc_and_docx(self):
        doc = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
        with mock.patch.object(watch, "fetch", return_value=FakeResp(200, content=doc)):
            out = watch.document_text("https://www.aod.rs/uploads/dokumenti/oglas/1_1_oglas.doc")
        self.assertEqual(out["format"], "doc")
        self.assertEqual(out["text"], "")
        self.assertIsNone(out["pdf"])
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", "<w:document><w:p><w:t>Alta banka isplata 50.000 evra</w:t></w:p></w:document>")
        with mock.patch.object(watch, "fetch", return_value=FakeResp(200, content=buf.getvalue())):
            out = watch.document_text("https://www.aod.rs/uploads/dokumenti/oglas/1_1_oglas.docx")
        self.assertEqual(out["format"], "docx")
        self.assertIn("50.000", out["text"])

    def test_scan_pdf_kept_for_model(self):
        from pypdf import PdfWriter
        import io
        w = PdfWriter()
        w.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        w.write(buf)
        with mock.patch.object(watch, "fetch", return_value=FakeResp(200, content=buf.getvalue())):
            out = watch.document_text("https://www.aod.rs/uploads/dokumenti/oglas/2_1_oglas.pdf")
        self.assertEqual(out["format"], "pdf")
        self.assertEqual(out["text"], "")
        self.assertIsNotNone(out["pdf"])
        self.assertEqual(out["pages"], 1)


class TestRecheck(unittest.TestCase):
    def test_added_paragraphs(self):
        html_v1 = (FIX / "aod_vest_v1.html").read_text("utf-8")
        html_v2 = (FIX / "aod_vest_v2.html").read_text("utf-8")
        with mock.patch.object(watch, "get", return_value=html_v1):
            a = watch.parse_article("https://www.aod.rs/clan/vest/vest_forma/137")
        with mock.patch.object(watch, "get", return_value=html_v2):
            b = watch.parse_article("https://www.aod.rs/clan/vest/vest_forma/137")
        self.assertEqual(a["kind"], "news")
        self.assertEqual(a["source"], "aod_news")
        self.assertEqual(watch.added_paragraphs(a["paras"], a), [])
        added = watch.added_paragraphs(a["paras"], b)
        self.assertEqual(len(added), 1)
        self.assertIn("banka isplatilac", added[0])
        self.assertNotEqual(a["hash"], b["hash"])


class TestTelegramFormatting(unittest.TestCase):
    def test_split_message(self):
        text = "\n\n".join(f"<b>блок {i}</b> " + "слово " * 300 for i in range(6))
        parts = watch.split_message(text)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(watch._visible_len(p), watch.TG_LIMIT)

    def test_model_strings_escaped(self):
        item = {"url": "https://n1info.rs/a", "kind": "press", "outlet": "N1", "date": "31.10.2026",
                "llm": {"title_ru": "<script>x</script> & заголовок", "summary_ru": ["a < b"], "relevance": 8},
                "tags": ["выплаты", "<img>"]}
        post = watch.format_post(item)
        self.assertNotIn("<script>", post)
        self.assertIn("&lt;script&gt;", post)
        self.assertIn("#выплаты", post)
        self.assertNotIn("<img>", post)


class TestMergeState(unittest.TestCase):
    def test_all_sections_survive(self):
        ours = watch.empty_state()
        theirs = watch.empty_state()
        ours["seen"]["u1"] = {"status": "sent"}
        theirs["seen"]["u1"] = {"status": "retry"}
        theirs["seen"]["u2"] = {"status": "retry"}
        ours["recent"] = [{"ts": 1.0, "text": "a", "url": "u1"}]
        theirs["pending"] = {"press": [{"url": "u2", "ts": 2.0}]}
        theirs["retry_items"] = {"u2": {"url": "u2", "kind": "press"}, "u1": {"url": "u1", "kind": "press"}}
        ours["facts"] = {"payout_bank": {"value": "X", "at": "2026-10-31T10:00:00+00:00"}}
        theirs["facts"] = {"payout_bank": {"value": "Y", "at": "2026-10-31T12:00:00+00:00"}}
        with tempfile.TemporaryDirectory() as d:
            pa, pb, po = Path(d, "a.json"), Path(d, "b.json"), Path(d, "o.json")
            pa.write_text(json.dumps(ours)), pb.write_text(json.dumps(theirs))
            subprocess.run([sys.executable, str(ROOT / "merge_state.py"), str(pa), str(pb), str(po)], check=True, capture_output=True)
            out = json.loads(po.read_text())
        self.assertEqual(out["seen"]["u1"]["status"], "sent")
        self.assertEqual(len(out["recent"]), 1)
        self.assertEqual(out["pending"]["press"][0]["url"], "u2")
        self.assertEqual(set(out["retry_items"]), {"u2"})          # u1 уже отправлен — из очереди убран
        self.assertEqual(out["facts"]["payout_bank"]["value"], "Y")  # более поздняя запись побеждает


class TestMainEndToEnd(unittest.TestCase):
    """Сквозной прогон main() на фикстурах: модель недоступна → ничего не теряем; модель вернулась → публикуем."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        Path("state").mkdir()
        watch.STATE_FILE = Path("state/dry_run_state.json")
        state = watch.empty_state()
        state["seeded"] = True
        watch.save_state(state)
        os.environ["ANTHROPIC_API_KEY"] = "test"
        watch.LLM_DOWN, watch.LLM_NET_FAILS, watch.LLM_OK, watch.LLM_CALLS = None, 0, False, 0
        watch.RECHECK.clear()
        watch._T0 = time.monotonic()
        self.feed = (FIX / "press_feed.xml").read_text("utf-8").replace("@@TS@@", time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()))

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        watch.STATE_FILE = Path("state/dry_run_state.json")

    def fake_get(self, url):
        if url == "https://www.aod.rs/clan/vest/sve_vesti":
            return '<a href="/clan/vest/vest_forma/137">x</a>'
        if url == "https://www.aod.rs/":
            return "<html></html>"
        if url.endswith("vest_forma/137"):
            return (FIX / "aod_vest_v1.html").read_text("utf-8")
        if url.endswith("akt_oglasi"):
            return ""   # пустая страница объявлений
        if "n1info.rs/feed" in url:
            return self.feed
        if url.startswith("https://n1info.rs/vesti/"):
            return "<html><head><title>T</title></head><body><article><p>" + "Alta banka isplata depozita počinje. " * 5 + "</p></article></body></html>"
        return None

    def run_main(self, llm_resp):
        with mock.patch.object(watch, "get", side_effect=self.fake_get), \
             mock.patch.object(watch, "collect_telegram", return_value=[]), \
             mock.patch.object(watch, "check_aod_proceedings", return_value=False), \
             mock.patch.object(watch.requests, "post", return_value=llm_resp) as post, \
             mock.patch.object(watch.time, "sleep"):
            watch.PRESS_FEEDS[:] = [f for f in watch.PRESS_FEEDS if f["id"] == "n1"]
            rc = watch.main()
        self.assertEqual(rc, 0)
        return post, json.loads(watch.STATE_FILE.read_text("utf-8"))

    def test_outage_then_recovery(self):
        billing = FakeResp(400, json.dumps({"error": {"message": "credit balance is too low"}}))
        post, st = self.run_main(billing)
        self.assertEqual(post.call_count, 1, "после первой системной ошибки в API не ходим")
        press = {u: r for u, r in st["seen"].items() if r.get("kind") == "press"}
        self.assertEqual(len(press), 2)
        for r in press.values():
            self.assertEqual(r["status"], "retry")
            self.assertEqual(r.get("attempts", 0), 0)
        self.assertEqual(len(st["retry_items"]), 2)
        self.assertIn("модель", st["health"]["alerts"])
        # официальная новость тоже отложена без счёта попыток
        self.assertEqual(st["seen"]["https://www.aod.rs/clan/vest/vest_forma/137"]["status"], "retry")

        # ленты опустели, модель вернулась: публикуем из очереди повторов, алерт закрыт
        self.feed = self.feed.replace("<item>", "<item-x>").replace("</item>", "</item-x>")
        watch.LLM_DOWN, watch.LLM_NET_FAILS, watch.LLM_OK, watch.LLM_CALLS = None, 0, False, 0
        watch._T0 = time.monotonic()
        post, st = self.run_main(FakeResp(200, js=llm_body()))
        statuses = {u: r["status"] for u, r in st["seen"].items() if r.get("kind") in ("press", "news")}
        self.assertEqual(statuses["https://www.aod.rs/clan/vest/vest_forma/137"], "sent")
        self.assertEqual(sorted(statuses[u] for u in statuses if "n1info" in u), ["dup", "sent"],
                         "вторая статья — та же заголовочная новость, дубль без вызова модели")
        self.assertEqual(st["retry_items"], {})
        self.assertNotIn("модель", st["health"]["alerts"])
        self.assertEqual(st["facts"], {})

    def test_recheck_posts_only_added_topical_text(self):
        st = json.loads(watch.STATE_FILE.read_text("utf-8"))
        aod = "https://www.aod.rs/clan/vest/vest_forma/137"
        st["seen"][aod] = {"status": "sent", "hash": "stale-hash-from-v2.0", "message_id": 5}   # запись без отпечатков абзацев
        watch.save_state(st)
        self.feed = self.feed.replace("<item>", "<item-x>").replace("</item>", "</item-x>")
        # 1) первый прогон новой версии: hash отличается, но отпечатков ещё нет → запоминаем, не публикуем
        post, st = self.run_main(FakeResp(200, js=llm_body()))
        self.assertEqual(post.call_count, 0)
        self.assertTrue(st["seen"][aod].get("paras"))
        # 2) страница не менялась → тишина
        watch.LLM_CALLS = 0
        post, st = self.run_main(FakeResp(200, js=llm_body()))
        self.assertEqual(post.call_count, 0)
        # 3) AOD дописала абзац про банк-выплатчик → один пост «дополнено», модели отдан только новый текст
        v2 = (FIX / "aod_vest_v2.html").read_text("utf-8")
        watch.LLM_CALLS = 0
        orig = self.fake_get
        self.fake_get = lambda u: v2 if u.endswith("vest_forma/137") else orig(u)
        body = llm_body(facts=[{"key": "payout_bank", "value": "Поштанска штедионица"}, {"key": "payout_start", "value": "05.11.2026"}])
        post, st = self.run_main(FakeResp(200, js=body))
        self.assertEqual(post.call_count, 1)
        sent_text = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("banka isplatilac", sent_text)
        self.assertNotIn("Извршни одбор Народне банке", sent_text)   # старые абзацы модели не шлём
        self.assertEqual(st["seen"][aod]["status"], "sent")
        self.assertEqual(set(st["facts"]), {"payout_bank", "payout_start"})
        self.assertEqual(st["facts"]["payout_bank"]["url"], aod)
        # 4) и снова тишина
        watch.LLM_CALLS = 0
        post, st = self.run_main(FakeResp(200, js=body))
        self.assertEqual(post.call_count, 0)

    def test_press_post_cap(self):
        many = []
        for i in range(6):
            many.append(f"<item><title>Alta banka: isplata vest {i} različita potpuno {i*7}</title><link>https://n1info.rs/vesti/p{i}</link>"
                        f"<description>Alta banka isplata depozita</description><pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}</pubDate></item>")
        self.feed = self.feed.split("<item>")[0] + "".join(many) + "</channel></rss>"
        post, st = self.run_main(FakeResp(200, js=llm_body()))
        press = {u: r for u, r in st["seen"].items() if r.get("kind") == "press"}
        by_status: dict[str, int] = {}
        for r in press.values():
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        # 6 важных статей: не больше PRESS_POSTS_PER_RUN отдельных постов за прогон,
        # дальше — накопитель подборки; на 6-ю не хватило бюджета модели (1 вызов ушёл на новость AOD,
        # прессе остаётся LLM_MAX_CALLS - LLM_RESERVE_OFFICIAL) — она ждёт без счёта попыток
        self.assertEqual(by_status["sent"], watch.PRESS_POSTS_PER_RUN)
        self.assertEqual(by_status["pending"], 2)
        self.assertEqual(by_status["retry"], 1)
        waiting = next(r for r in press.values() if r["status"] == "retry")
        self.assertEqual(waiting["reason"], "llm_budget")
        self.assertEqual(waiting.get("attempts", 0), 0)
        self.assertEqual(post.call_count, watch.LLM_MAX_CALLS - watch.LLM_RESERVE_OFFICIAL)
        self.assertTrue(all(p["important"] for p in st["pending"]["press"]))


if __name__ == "__main__":
    unittest.main()
