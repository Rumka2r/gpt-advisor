#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Единый вход в историю разговоров: смысл + точные слова в одной выдаче.

Раньше было два несвязанных инструмента, и надо было угадывать, каким искать:
  episodes.py — понимает суть, слеп к точным строкам («200015081857263»);
  fts.py      — находит строку, но не понимает «чем закончилось».
Здесь оба работают на каждый запрос, а результаты сводятся к одной единице —
эпизоду — и складываются.

Слияние обратно-ранговое: вклад находки равен 1/(K+место). Это не требует
приводить к общей шкале несравнимые оценки (косинус и BM25) и мягко
поднимает то, что нашли оба искателя сразу.

Выдача всегда несёт якорь, по которому window.py дочитает исходный текст.
"""

import argparse
import functools
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402
import health as health_mod  # noqa: E402
import episodes  # noqa: E402
import fts  # noqa: E402

RRF_K = 60          # сглаживание: чем больше, тем ровнее вклад мест
POOL = 25           # сколько брать из каждого искателя до слияния


def _episode_of(con, uuid):
    r = con.execute("SELECT episode_id FROM ep_uuid WHERE uuid=?", (uuid,)).fetchone()
    return r[0] if r else None


@functools.lru_cache(maxsize=512)
def _doc_status(path):
    """Чем является файл памяти: действующим фактом, снимком, справкой, историей.

    🔴 До 05.08 ЛЮБОЙ найденный файл подписывался «действующий факт, не история».
    Это неправда: в сторе лежат и снимки состояния, и дневниковые записи за май.
    Архитектор поймал это на `recent-home.md` — майский файл шёл как действующий.

    Определяем по имени и по `type:` из frontmatter, без гаданий: чего не знаем,
    так и помечаем «неизвестно», чтобы проверяли глазами.
    """
    try:
        name = os.path.basename(path).lower()
        # дневники и ленты: «2026-05-02.md», «recent-home.md»
        if re.match(r"^\d{4}-\d{2}-\d{2}", name) or name.startswith("recent"):
            return "история"
        # В базе путь лежит относительным («memory\private\…») — сам по себе он
        # не открывается, и статус выходил «неизвестно» для всего подряд.
        if not os.path.isabs(path):
            base = os.path.expanduser("~/.claude/projects/C--Users-andri-work-osha")
            path = os.path.join(base, path.replace("\\", os.sep))
        text = open(path, encoding="utf-8", errors="ignore").read(700)
        # `type:` лежит с отступом внутри блока metadata — якорь начала строки
        # без \s* не срабатывал, и всё подряд помечалось «неизвестно».
        m = re.search(r"^\s*type:\s*(\w+)", text, re.M)
        kind = (m.group(1) if m else "").lower()
        if kind == "reference":
            return "справочник"
        if kind in ("feedback", "user"):
            return "действует"
        if kind == "project":
            # 🔴 Возраст берём из ДАТЫ В ИМЕНИ, если она есть, а не из mtime:
            # файлы трогают линтеры и хуки, и майский снимок выглядел свежим.
            age_d = None
            dm = re.search(r"(20\d{2})[-_](\d{2})[-_](\d{2})", name)
            if dm:
                import datetime as _d
                try:
                    made = _d.date(*(int(x) for x in dm.groups()))
                    age_d = (_d.date.today() - made).days
                except ValueError:
                    age_d = None
            if age_d is None:
                age_d = (time.time() - os.path.getmtime(path)) / 86400
            return "действует" if age_d <= 14 else "снимок"
    except Exception:
        pass
    return "неизвестно"


def search(query, k=6, con=None, want_semantic=True, want_exact=True,
           exclude_session=None, health=None):
    """exclude_session — не искать в самом себе.

    Сессия, в которой сейчас идёт разговор, содержит все слова запроса просто
    потому, что запрос в ней и прозвучал. Без этого отсева выдача забивается
    эхом текущего разговора вместо настоящей истории.
    """
    con = con or catalog.db()
    con.row_factory = sqlite3.Row
    skip = (exclude_session or "")[:8]
    merged = {}          # episode_id -> запись

    def bump(eid, rank, source, extra=None):
        rec = merged.setdefault(eid, {"score": 0.0, "sources": set(), "frag": None, "hit_uuid": None})
        rec["score"] += 1.0 / (RRF_K + rank)
        rec["sources"].add(source)
        if extra and not rec["frag"]:
            rec["frag"] = extra.get("frag")
            rec["hit_uuid"] = extra.get("uuid")

    # 1) смысловой — уже в единицах эпизодов
    if want_semantic:
        try:
            for rank, h in enumerate(episodes.search(query, k=POOL, con=con)):
                r = con.execute("SELECT id FROM episodes WHERE start_uuid=?", (h["start_uuid"],)).fetchone()
                if r:
                    bump(r[0], rank, f"смысл/{h['matched']}")
        except Exception as e:
            print(f"[смысловой поиск недоступен: {type(e).__name__}]", file=sys.stderr)
            if health:
                health.fail("смысловой поиск", e)

    # 2) точный — приходит событиями, приводим к эпизодам
    # 🔴 Обёрнут так же, как смысловой: до 05.08 падение FTS (повреждённая или
    # заблокированная таблица) выбрасывало исключение наружу вместо статуса
    # partial/failed, а имя стадии «точный поиск» не выставлял никто — значит
    # состояние failed было недостижимо в принципе (нашёл архитектор).
    loose = []
    if want_exact:
        try:
            for rank, h in enumerate(fts.search(query, k=POOL, con=con)):
                eid = _episode_of(con, h["uuid"])
                if eid:
                    bump(eid, rank, "слова", {"frag": h["frag"], "uuid": h["uuid"]})
                else:
                    loose.append(h)   # событие вне эпизодов — покажем отдельно
        except Exception as e:
            print(f"[точный поиск недоступен: {type(e).__name__}]", file=sys.stderr)
            if health:
                health.fail("точный поиск", e)

    # Квота для точного поиска. Причина (совет архитектора 04.08): при росте
    # корпуса новые семантически похожие эпизоды вытесняют старую ТОЧНУЮ
    # находку — редкий термин («8899») перестаёт находиться, хотя он в базе.
    # Поэтому часть мест закрепляется за совпадением по словам.
    EXACT_QUOTA = max(1, k // 3)
    ranked = sorted(merged.items(), key=lambda x: -x[1]["score"])
    exact_ids = [eid for eid, rec in ranked if "слова" in rec["sources"]][:EXACT_QUOTA]
    order = [p for p in ranked if p[0] in exact_ids] + \
            [p for p in ranked if p[0] not in exact_ids]

    out = []
    for eid, rec in order:
        if len(out) >= k:
            break
        e = con.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
        if not e:
            continue
        if skip and e["session"][:8] == skip:
            continue
        # Нить, к которой относится находка: показывает, что дело тянулось
        # дальше этой сессии, и даёт вход в его хронологию целиком.
        th = con.execute(
            "SELECT t.id, t.title, t.episodes, t.sessions, t.first_ts, t.last_ts "
            "FROM work_threads t JOIN thread_episode te ON te.thread_id=t.id "
            "WHERE te.episode_id=?", (eid,)).fetchone()
        thread, newer = None, None
        if th and th["sessions"] > 1:
            thread = {"id": th["id"], "title": th["title"], "episodes": th["episodes"],
                      "sessions": th["sessions"],
                      "from": (th["first_ts"] or "")[:10], "to": (th["last_ts"] or "")[:10]}
        if th:
            # Что было в этом деле ПОЗЖЕ найденного. История хранит всё, но
            # действующим считается последнее: без этой пометки легко подать
            # отменённое решение как актуальное.
            nx = con.execute(
                "SELECT e.start_ts, e.outcome_text, e.facts, e.title, e.start_uuid FROM episodes e "
                "JOIN thread_episode te ON te.episode_id=e.id "
                "WHERE te.thread_id=? AND e.start_ts > ? "
                "ORDER BY e.start_ts DESC LIMIT 1", (th["id"], e["start_ts"] or "")).fetchone()
            if nx:
                # предпочитаем факты: в них конкретика (даты, суммы, номера),
                # а общий текст ответа часто про параллельное дело
                later = nx["facts"] or nx["outcome_text"] or nx["title"] or ""
                newer = {"when": (nx["start_ts"] or "")[:16].replace("T", " "),
                         "anchor": nx["start_uuid"],
                         "outcome": " ".join(later.split())[:220]}
        out.append({
            "thread": thread,
            "newer": newer,
            "score": round(rec["score"], 4),
            "sources": sorted(rec["sources"]),
            "both": len(rec["sources"]) > 1,
            "title": e["title"],
            "when": (e["start_ts"] or "")[:16].replace("T", " "),
            "session": e["session"][:8],
            "project": e["project"],
            "blocks": e["blocks"],
            "files": e["files"],
            "outcome": " ".join((e["outcome_text"] or "").split())[:280],
            "facts": " ".join((e["facts"] or "").split())[:400],
            # Факты с якорями: любое утверждение можно открыть в первоисточнике
            # и убедиться, что оно сказано именно так, а не пересказано.
            "evidence": [{"text": f["text"], "anchor": f["uuid"]} for f in con.execute(
                "SELECT text, uuid FROM ep_fact WHERE episode_id=? LIMIT 6", (eid,))],
            "anchor": rec["hit_uuid"] or e["start_uuid"],
            "frag": rec["frag"],
            "kind": "история",
        })

    # Слоты фактов — самый верхний уровень достоверности: значение, которое
    # действует прямо сейчас, с ссылкой на первоисточник и на то, что оно
    # заменило. Ищем по словам запроса в теме и признаке слота.
    try:
        import claims
        # слова от трёх букв: «шин», «код», «VIN» — как раз то, чем называют
        # предмет слота, и отсекать их нельзя
        qwords = {w for w in re.findall(r"\w{3,}", query.lower(), re.U)}
        try:
            import translit
            qwords |= set(translit.expand(query))
        except Exception:
            pass
        for c in claims.active(con=con):
            hay = f"{c['subject']} {c['predicate']} {c['value']}".lower()
            hit = sum(1 for w in qwords if w[:4] in hay)
            if hit >= 2:
                # Просроченное значение остаётся в выдаче, но НЕ как «действует
                # сейчас»: слот «текущая печать» 05.08 девятнадцать часов врал
                # именно так. И вниз — чтобы не занимало первую строку.
                stale, age_h = claims.freshness(c)
                out.insert(0, {
                    # 🔴 Отдельный вид, а не просто пометка: в сортировке «факт»
                    # стоит первым, и просроченное значение всё равно оказывалось
                    # верхней строкой (замечание архитектора 05.08).
                    "kind": "протухший факт" if stale else "факт",
                    "score": (0.5 if stale else 1.0) + hit / 100,
                    "sources": ["слот"], "both": False,
                    "title": f"{c['subject']} · {c['predicate']}",
                    "when": (c["recorded_at"] or "")[:16].replace("T", " "),
                    "session": "", "project": "", "facts": "", "evidence": [],
                    "outcome": c["value"],
                    "anchor": c["source_uuid"] or "", "frag": None,
                    "thread": None, "newer": None,
                    "stale": stale, "age_h": age_h,
                    "claim_class": c["claim_class"] if "claim_class" in c.keys() else None,
                })
    except Exception as e:
        print(f"[слоты фактов недоступны: {type(e).__name__}]", file=sys.stderr)
        if health:
            health.fail("слоты фактов", e)

    # 🔴 Точные находки ВНЕ эпизодов. Они собирались в `loose` и... нигде не
    # использовались — просто исчезали (нашёл архитектор 05.08). А это ровно
    # то, что нужнее всего: события субагентов, свежие события, ещё не попавшие
    # в эпизоды, и всё, что сегментатор в эпизод не включил.
    if want_exact and loose:
        seen_uuid = {h.get("anchor") for h in out}
        for h in loose[:max(2, k // 3)]:
            if h["uuid"] in seen_uuid:
                continue
            out.append({
                "kind": "событие", "score": 0.4, "sources": ["слова"], "both": False,
                "title": (h.get("frag") or "")[:80] or "событие вне эпизода",
                "when": (h.get("ts") or "")[:16].replace("T", " "),
                "session": (h.get("session") or "")[:8], "project": "", "facts": "",
                "evidence": [], "outcome": (h.get("frag") or "")[:280],
                "anchor": h["uuid"], "frag": h.get("frag"),
                "thread": None, "newer": None,
            })

    # ПРОДУКТЫ РАБОТЫ. Разговоры отвечают «что обсуждали», артефакты — «что
    # сделано и где лежит». 05.08 ответ («папка big_06_rev, вчера 21:32») не
    # находился ни одним искателем, потому что файловую систему не знал никто.
    try:
        import artifacts
        recency = re.search(r"вчера|позавчера|сегодн|последн|свеж|недавн|только что|"
                            r"сейчас|что.{0,12}делал", query.lower())
        found = artifacts.search(query, k=4, con=con)
        if recency and len(found) < 3:
            # спросили про недавнее — показываем свежие папки, даже если
            # ни одно слово запроса не встретилось в их именах
            seen = {r["path"] for r in found}
            found += [r for r in artifacts.recent(hours=48, limit=4, con=con)
                      if r["path"] not in seen]
        for r in found[:4]:
            import time as _t
            when = _t.strftime("%d.%m %H:%M", _t.localtime(r["mtime"]))
            what = (f'папка, {r["files"]} файлов' if r["kind"] == "папка"
                    else f'файл, {(r["size"] or 0) // 1024} КБ')
            out.append({
                "kind": "артефакт", "score": 0.9, "sources": ["файлы"], "both": False,
                "title": r["path"], "when": when,
                "session": "", "project": r["project"] or "", "facts": "",
                "evidence": [], "outcome": what,
                "anchor": r["path"], "frag": None, "thread": None, "newer": None,
            })
    except Exception as e:
        print(f"[индекс артефактов недоступен: {type(e).__name__}]", file=sys.stderr)
        if health:
            health.fail("индекс артефактов", e)

    # Точный поиск ПО ПАМЯТИ. Отдельно от смыслового: порт, адрес, версия
    # записаны в памяти, но запрос про них семантически далёк от текста
    # раздела. Под флагом — включается замером, а не на веру.
    if want_exact and os.environ.get("MEM_FTS", "1") != "0":
        try:
            for m in fts.search_memory(query, k=max(2, k // 3), con=con):
                out.append({
                    "kind": "память", "score": 0.5, "sources": ["слова/память"],
                    "both": False, "title": f"{m['title']} › {(m['section'] or '')[:60]}",
                    "when": "", "session": "", "project": "",
                    "facts": "", "evidence": [], "outcome": m["frag"],
                    "anchor": m["path"], "frag": m["frag"],
                    "thread": None, "newer": None,
                    "doc_status": _doc_status(m["path"]),
                })
        except Exception as e:
            print(f"[точный поиск по памяти недоступен: {type(e).__name__}]", file=sys.stderr)
            if health:
                health.fail("точный поиск по памяти", e)

    # Файлы памяти — отдельный источник и другой статус: там записано то, что
    # считается верным СЕЙЧАС, тогда как эпизод говорит лишь «так было тогда».
    # Часть фактов (сроки офферов, адреса, номера) в разговорах вообще не
    # звучит — они сразу записывались в память.
    if want_semantic:
        try:
            import memdocs
            # берём из памяти шире: куски мелкие, и нужный факт нередко
            # оказывается третьим-четвёртым, уступив по формулировке
            for m in memdocs.search(query, k=max(4, k // 2), con=con):
                if m["score"] >= 0.45:
                    out.append({
                        "kind": "память", "score": round(m["score"], 3),
                        "sources": ["память"], "both": False,
                        "title": f"{m['title']} › {m['section'][:60]}",
                        "when": "", "session": "", "project": "",
                        "facts": "", "evidence": [], "outcome": m["text"][:280],
                        "anchor": m["path"], "frag": None,
                        "thread": None, "newer": None,
                        "doc_status": _doc_status(m["path"]),
                    })
        except Exception as e:
            print(f"[поиск по памяти недоступен: {type(e).__name__}]", file=sys.stderr)
            if health:
                health.fail("поиск по памяти", e)

    # Уверенная находка в памяти идёт первой: это ответ на «как сейчас», а
    # эпизод отвечает лишь на «как было тогда». Оценки у источников в разных
    # шкалах (косинус против рангового слияния), поэтому сравниваем не числа,
    # а статус: сначала действующий факт, затем история.
    # Фрагмент под вопрос: в выдачу должно попадать то место эпизода, где
    # стоит ответ, а не его начало (диагноз 04.08: приводим в 94%, показываем
    # в 44%). Под флагом, чтобы вклад можно было измерить абляцией.
    if os.environ.get("ANSWER_SPAN", "1") != "0":
        try:
            enrich(out, query, con)
        except Exception as e:
            print(f"[выделение ответа недоступно: {type(e).__name__}]", file=sys.stderr)
            if health:
                health.fail("выделение ответа", e)

    order = {"факт": 0, "артефакт": 1, "память": 2,
             "протухший факт": 3, "история": 4, "событие": 5}
    out.sort(key=lambda h: (order.get(h.get("kind"), 6),
                            -h["score"] if h.get("kind") in ("факт", "память", "протухший факт") else 0))
    _ = (lambda h: (h.get("kind") != "память" or h["score"] < 0.5,
                            -h["score"] if h.get("kind") == "память" else 0))
    return out


def render(hits):
    if not hits:
        return "ничего не нашлось"
    lines = []
    for h in hits:
        if h.get("kind") in ("факт", "протухший факт"):
            subj = h["title"].split(" · ")[0]
            if h.get("stale"):
                age = f", ему {h['age_h']:.0f} ч" if h.get("age_h") else ""
                lines.append(f"\n[СЛОТ] {h['title']} — ⚠ ПОСЛЕДНЕЕ ИЗВЕСТНОЕ на "
                             f"{h['when']}{age}, актуальность НЕ подтверждена")
                lines.append(f"   {h['outcome']}")
                lines.append(f"   проверь, прежде чем говорить как о текущем; подтвердить: "
                             f"memoryctl.py claim verify \"{subj}\" \"{h['title'].split(' · ')[-1]}\"")
            else:
                lines.append(f"\n[ФАКТ] {h['title']} — действует сейчас")
                lines.append(f"   {h['outcome']}")
            if h["anchor"]:
                lines.append(f"   первоисточник: {h['anchor']}")
            lines.append(f"   история замен: memoryctl.py claim history \"{subj}\"")
            continue
        if h.get("kind") == "событие":
            lines.append(f"\n[СОБЫТИЕ] {h['when']} — точное совпадение вне эпизода")
            lines.append(f"   {h['outcome'][:200]}")
            lines.append(f"   якорь: {h['anchor']}   (дочитать: memoryctl.py window {h['anchor']})")
            continue
        if h.get("kind") == "артефакт":
            lines.append(f"\n[РАБОТА] {h['when']} — {h['outcome']}")
            lines.append(f"   {h['title']}")
            continue
        if h.get("kind") == "память":
            # 🔴 Не всякий файл памяти — «действующий факт»: в сторе лежат и
            # снимки состояния, и справочники, и прямо устаревшее (нашёл
            # архитектор 05.08 — майский recent-home.md шёл как действующий).
            st = h.get("doc_status") or "неизвестно"
            mark = {"действует": "ПАМЯТЬ — действующий факт",
                    "снимок": "ПАМЯТЬ — СНИМОК состояния, мог устареть",
                    "справочник": "ПАМЯТЬ — справочник",
                    "история": "ПАМЯТЬ — ИСТОРИЯ, не текущее",
                    "неизвестно": "ПАМЯТЬ — статус файла неизвестен, проверь дату"}[st]
            lines.append(f"\n[{h['score']:.2f}] {mark}")
            lines.append(f"   {h['title']}")
            lines.append(f"   {h['outcome'][:230]}")
            lines.append(f"   файл: {h['anchor']}")
            continue
        mark = " ★ нашли оба" if h["both"] else ""
        lines.append(f"\n[{h['score']:.4f}] {h['when']}  ({', '.join(h['sources'])}){mark}  "
                     f"— история, могло устареть")
        lines.append(f"   {h['title']}")
        if h["frag"]:
            lines.append(f"   совпало: {h['frag'][:190]}")
        if h.get("answer_frag"):
            lines.append(f"   ★ ОТВЕТ: {h['answer_value']}")
            lines.append(f"     {h['answer_frag'][:230]}")
        ev = h.get("evidence") or []
        if ev:
            lines.append("   факты (дословно, каждый проверяем по своему якорю):")
            for f in ev[:3]:
                lines.append(f"     • {f['text'][:150]}")
                lines.append(f"       ↳ {f['anchor']}")
        elif h["outcome"]:
            lines.append(f"   итог: {h['outcome'][:190]}")
        n = h.get("newer")
        if n:
            lines.append(f"   ⚠ НЕ ПОСЛЕДНЕЕ СЛОВО. Позже, {n['when']}, в этом же деле: "
                         f"{n['outcome'][:150]}")
        t = h.get("thread")
        if t:
            lines.append(f"   нить #{t['id']}: «{t['title'][:60]}» — {t['episodes']} эпизодов "
                         f"в {t['sessions']} сессиях, {t['from']}→{t['to']} "
                         f"(вся история: memoryctl.py thread --show {t['id']})")
        lines.append(f"   якорь: {h['anchor']}   (дочитать: memoryctl.py window {h['anchor']})")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Поиск по истории: смысл + точные слова")
    ap.add_argument("query")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", choices=["смысл", "слова"], help="отключить второй искатель")
    ap.add_argument("--exclude", help="не искать в этой сессии (обычно — текущей)")
    a = ap.parse_args()

    # Честный статус прогона: какие стадии отвалились и насколько свеж индекс.
    # Без этого пустая выдача неотличима от «такого не было» (разбор 05.08).
    h = health_mod.Health()
    con = catalog.db()
    hits = search(a.query, a.k, con=con,
                  want_semantic=(a.only != "слова"),
                  want_exact=(a.only != "смысл"),
                  exclude_session=a.exclude, health=h)
    h.watermark(con)
    h.save()

    if a.json:
        print(json.dumps({**h.as_dict(), "results": hits}, ensure_ascii=False, indent=1))
    else:
        banner = h.banner(empty=not hits)
        if banner:
            print(banner)
        print(render(hits))

    # Ненулевой код — чтобы «пусто при сломанной памяти» нельзя было принять
    # за «пусто, потому что нет». ok → 0, partial → 2, failed → 3.
    return {"ok": 0, "partial": 2, "failed": 3}[h.status]


def trace(query, k=6, con=None, exclude_session=None):
    """Разбор одного запроса: что и откуда пришло, почему выбралось.

    Совет архитектора 04.08: без трассировки нельзя отличить «факта нет в
    базе» от «факт есть, но проиграл ранжирование». Это разные болезни, и
    лечатся они по-разному.
    """
    con = con or catalog.db()
    con.row_factory = sqlite3.Row
    rep = {"запрос": query}

    try:
        import entities
        rep["распознанные сущности"] = entities.resolve(query, con) or "—"
    except Exception:
        rep["распознанные сущности"] = "модуль недоступен"
    try:
        import translit
        rep["варианты написания"] = translit.expand(query) or "—"
    except Exception:
        pass
    rep["выражение точного поиска"] = fts._q(query) or "—"

    try:
        ex = fts.search(query, k=8, con=con)
        rep["точный поиск: событий"] = len(ex)
        rep["точный поиск: верх"] = [f"{h['ts']} {h['frag'][:70]}" for h in ex[:3]]
    except Exception as e:
        rep["точный поиск"] = f"ошибка {type(e).__name__}"
    try:
        sem = episodes.search(query, k=8, con=con)
        rep["смысловой: эпизодов"] = len(sem)
        rep["смысловой: верх"] = [f"{h['score']:.2f} по {h['matched']} · {h['title'][:60]}"
                                  for h in sem[:3]]
    except Exception as e:
        rep["смысловой"] = f"ошибка {type(e).__name__}"
    try:
        rep["точный по памяти"] = [f"{m['title']} › {(m['section'] or '')[:40]}"
                                   for m in fts.search_memory(query, k=3, con=con)] or "—"
    except Exception:
        pass

    final = search(query, k=k, con=con, exclude_session=exclude_session)
    rep["итоговая выдача"] = [
        f"[{h.get('kind')}] {h['score']} · {', '.join(h['sources'])} · {h['title'][:60]}"
        for h in final]
    return rep


def render_trace(rep):
    lines = []
    for key, val in rep.items():
        if isinstance(val, list):
            lines.append(f"{key}:")
            lines.extend(f"    {v}" for v in val)
        else:
            lines.append(f"{key}: {val}")
    return "\n".join(lines)


# --- показ ответа ---------------------------------------------------------
# Диагноз 04.08 (подтверждён архитектором): система ПРИВОДИТ в нужный эпизод в
# 94% случаев, но ответ виден сразу лишь в 44%. Значит узкое место не поиск, а
# показ: во фрагмент попадает начало эпизода, а не то место, где стоит ответ.
#
# Здесь фрагмент выбирается ПОД ВОПРОС: определяем, значение какого типа
# спрашивают, ищем его в тексте эпизода и показываем предложение вокруг него.
# Извлечение детерминированное — модель не должна сочинять отсутствующее.

ASK_TYPES = [
    ("сумма", re.compile(r"сколько|цен[ауые]|стоим|стоит|дорог|дешев|дешёв|плат|"
                         r"бюджет|долл|почём", re.I),
     re.compile(r"\$\s?\d[\d.,]{1,9}")),
    ("время", re.compile(r"во\s+сколько|час[уыоа]?\b|время|когда|успе|запис|"
                         r"назначен|приём|прием|финиш", re.I),
     re.compile(r"\b\d{1,2}:\d{2}\b")),
    ("номер", re.compile(r"номер|заказ|брон|подтвержд|трекинг|отслеж|идентификат", re.I),
     re.compile(r"\b\d{6,20}\b")),
    ("адрес", re.compile(r"\bip\b|адрес|сервер|порт\b|хост", re.I),
     re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\b\d{4,5}\b")),
    ("дата", re.compile(r"како[гм]о\s+числа|дата|när|когда", re.I),
     re.compile(r"\b\d{1,2}\s?(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|"
                r"сентябр|октябр|ноябр|декабр)\w*", re.I)),
]


def answer_span(question, text, width=170):
    """Кусок текста вокруг значения того типа, о котором спрашивают.

    Возвращает (фрагмент, значение) либо (None, None). Ничего не выдумывает:
    если значения нужного типа в тексте нет, честно возвращает пусто.

    ⚠️ Здесь намеренно ПЕРВОЕ совпадение типа и первое значение этого типа.
    04.08 я пробовал «умнее»: ранжировать типы по уточняющим словам и выбирать
    кандидата ближе к словам вопроса. Замер на замороженном срезе показал
    ухудшение — 64% → 60% → 55%. Обе попытки откачены. Менять только с
    замером: интуиция здесь обманывает.
    """
    if not text:
        return None, None
    for _name, ask_rx, val_rx in ASK_TYPES:
        if not ask_rx.search(question):
            continue
        m = val_rx.search(text)
        if not m:
            continue
        a = max(0, m.start() - width // 2)
        b = min(len(text), m.end() + width // 2)
        frag = " ".join(text[a:b].split())
        return ("…" if a else "") + frag + ("…" if b < len(text) else ""), m.group(0)
    return None, None


def enrich(hits, question, con=None):
    """Дополняет находки фрагментом, где реально стоит ответ на вопрос."""
    con = con or catalog.db()
    con.row_factory = sqlite3.Row
    for h in hits:
        if h.get("kind") != "история":
            continue
        row = con.execute(
            "SELECT facts, outcome_text, detail_text, goal_text FROM episodes "
            "WHERE start_uuid=? OR uuids LIKE ?",
            (h["anchor"], f"%{h['anchor']}%")).fetchone()
        if not row:
            continue
        # порядок важен: сперва факты (там итоги), потом мои ответы, потом изнанка
        for field in ("facts", "outcome_text", "detail_text", "goal_text"):
            frag, val = answer_span(question, row[field])
            if frag:
                h["answer_frag"] = frag
                h["answer_value"] = val
                break
    return hits

# 🔴 Точка входа обязана стоять ПОСЛЕ всех объявлений: до 05.08 она стояла
# на строке 304, а enrich() объявлялась на 421 — при прямом запуске
# find.py падал NameError, и выделение ответа молча отключалось
# (нашёл GPT-архитектор 05.08, подтверждено по номерам строк).
if __name__ == "__main__":
    raise SystemExit(main())
