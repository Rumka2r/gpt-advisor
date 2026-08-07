# -*- coding: utf-8 -*-
"""Приёмка разграничения инстансов — 7 correctness-проверок из вердикта
архитектора 06.08.2026 (reply_999_identity_full.md). Не performance.

Запуск:  python test_identity.py
Работает в изолированном временном реестре (CONTINUITY_IDENTITY_DIR) и
временной папке резюме — боевой registry.sqlite и sessions/ не трогаются.
"""
import os
import sys
import tempfile
import uuid

TMP = tempfile.mkdtemp(prefix="identity_test_")
os.environ["CONTINUITY_IDENTITY_DIR"] = os.path.join(TMP, "identity")
os.makedirs(os.environ["CONTINUITY_IDENTITY_DIR"], exist_ok=True)
with open(os.path.join(os.environ["CONTINUITY_IDENTITY_DIR"], "agents.yaml"),
          "w", encoding="utf-8") as f:
    f.write("agents:\n"
            "  osha-main:\n    profile: osha\n"
            "  osha-999:\n    profile: osha\n"
            "  most:\n    profile: most\n")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import continuity_identity as ident  # noqa: E402
import now  # noqa: E402

now.SESSIONS = os.path.join(TMP, "sessions")
now.STATE = os.path.join(TMP, "state")
now.RECENT = os.path.join(now.STATE, "RECENT.md")
now.THREADS = os.path.join(now.STATE, "THREADS.md")
os.makedirs(now.SESSIONS, exist_ok=True)
os.makedirs(now.STATE, exist_ok=True)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("✅" if cond else "🔴") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


def be(agent, instance=None):
    """Притвориться процессом агента (как это делает launcher)."""
    os.environ[ident.ENV_AGENT] = agent
    os.environ[ident.ENV_INSTANCE] = instance or str(uuid.uuid4())


AGENTS = ["osha-main", "osha-999", "most"]
sids = {}          # agent -> [session_id, session_id]

# ── 1. Три агента, по две сессии у каждого ────────────────────────────────────
for ag in AGENTS:
    be(ag)
    iid, gen = ident.register_instance(ag)
    os.environ[ident.ENV_INSTANCE] = iid
    sids[ag] = []
    for i in range(2):
        sid = str(uuid.uuid4())
        st = ident.bind_session(sid, transcript_path=f"/x/{sid}.jsonl")
        assert st == "bound", st
        sids[ag].append(sid)
        # резюме сессии, как их пишет summarize.py
        fmid = "\n".join(ident.summary_identity_fm(sid))
        with open(os.path.join(now.SESSIONS, f"2026-08-06_{sid[:8]}.md"), "w",
                  encoding="utf-8") as f:
            f.write(f"---\nsession: {sid}\n{fmid}\nproject: C--Users-andri-work-osha\n"
                    f"agent: claude\ndate: 2026-08-06\ntitle: \"дело {ag} №{i+1}\"\n"
                    f"started: 2026-08-06 10:0{i}\nended: 2026-08-06 11:0{i}\nturns: 5\n---\n\n"
                    f"# дело {ag} №{i+1}\n\n## Суть\n- работа {ag} номер {i+1}\n\n"
                    f"## Итог\n- сделано\n\n## Открыто\n- нет\n")
# legacy-сессия до разграничения — без привязки
legacy_sid = str(uuid.uuid4())
with open(os.path.join(now.SESSIONS, f"2026-08-01_{legacy_sid[:8]}.md"), "w",
          encoding="utf-8") as f:
    f.write(f"---\nsession: {legacy_sid}\nproject: C--Users-andri-work-osha\nagent: claude\n"
            f"date: 2026-08-01\ntitle: \"старое общее дело\"\nstarted: 2026-08-01 09:00\n"
            f"ended: 2026-08-01 10:00\nturns: 3\n---\n\n# старое общее дело\n\n"
            f"## Суть\n- древность\n\n## Итог\n- было\n\n## Открыто\n- нет\n")

con = ident.db()
n = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
per = {ag: con.execute("SELECT COUNT(*) FROM sessions WHERE agent_id=?", (ag,)).fetchone()[0]
       for ag in AGENTS}
check("1. три агента имеют по две сессии",
      n == 6 and all(v == 2 for v in per.values()), f"{n=} {per=}")

# ── 2. SessionStart каждого получает только свои две ──────────────────────────
ok2 = True
for ag in AGENTS:
    be(ag)
    ctx = now.context_text()
    mine = all(f"дело {ag} №{i}" in ctx for i in (1, 2))
    foreign = any(f"дело {other} №" in ctx for other in AGENTS if other != ag)
    legacy_in = "старое общее дело" in ctx
    if not mine or foreign or legacy_in:
        ok2 = False
        print(f"   [{ag}] mine={mine} foreign={foreign} legacy={legacy_in}")
check("2. SessionStart инжектит только свои сессии (без чужих и без legacy)", ok2)

# ── 3. auto_recall без scope не возвращает чужие ──────────────────────────────
be("osha-main")
fake_hits = [
    {"kind": "история", "session": sids["osha-main"][0][:8], "title": "своё"},
    {"kind": "история", "session": sids["osha-999"][0][:8], "title": "чужое-999"},
    {"kind": "история", "session": sids["most"][0][:8], "title": "чужое-most"},
    {"kind": "история", "session": legacy_sid[:8], "title": "легаси"},
    {"kind": "память", "session": "", "title": "общий канон"},
]
kept, dropped = ident.apply_scope([dict(h) for h in fake_hits], scope="mine")
titles = {h["title"] for h in kept}
legacy_hit = next((h for h in kept if h["title"] == "легаси"), None)
check("3. auto_recall (scope=mine): чужие отсечены, свои+канон+legacy остались",
      titles == {"своё", "легаси", "общий канон"} and dropped == 2
      and legacy_hit and legacy_hit.get("owner_note") == "[владелец сессии неизвестен]",
      f"{titles=} {dropped=}")

# ── 4. Явный cross-agent поиск возвращает чужие с автором ─────────────────────
kept_all, _ = ident.apply_scope([dict(h) for h in fake_hits], scope="all")
h999 = next((h for h in kept_all if h["title"] == "чужое-999"), None)
kept_ag, _ = ident.apply_scope([dict(h) for h in fake_hits], agent="most")
ag_titles = {h["title"] for h in kept_ag}
check("4. explicit cross-agent: чужие видны и помечены автором",
      len(kept_all) == 5 and h999 and h999.get("owner_note") == "[автор: osha-999]"
      and ag_titles == {"чужое-most", "общий канон"},
      f"{[h.get('owner_note') for h in kept_all]=} {ag_titles=}")

# ── 5. Воскресший новый instance получает только нити своего agent_id ─────────
be("osha-main")
now.write_threads([
    ident.thread_line("нить главного", "osha-main"),
    ident.thread_line("нить девятьсот", "osha-999"),
    "старая общая нить",
])
be("osha-main", str(uuid.uuid4()))          # НОВЫЙ instance того же агента
v_main = {t for _, _, t in now.threads_view()}
be("most", str(uuid.uuid4()))               # другой агент
v_most = {t for _, _, t in now.threads_view()}
check("5. новый instance того же агента видит свои нити; другой агент — нет",
      v_main == {"нить главного", "старая общая нить"}
      and v_most == {"старая общая нить"}, f"{v_main=} {v_most=}")

# ── 6. Resume чужого session_id отклоняется ───────────────────────────────────
be("osha-999")
st = ident.bind_session(sids["osha-main"][0])
owner_after = ident.owner_of(sids["osha-main"][0])
check("6. resume чужой сессии → conflict, владелец не переписан",
      st == "conflict:osha-main" and owner_after == "osha-main", f"{st=} {owner_after=}")
be("osha-main")
check("6б. resume СВОЕЙ сессии разрешён",
      ident.bind_session(sids["osha-main"][0]) == "already")

# ── 7. Explicit transfer меняет владельца, старый её больше не получает ───────
be("osha-main")
view = now.threads_view()
idx = next(i for i, (_, o, t) in enumerate(view) if t == "нить главного")
file_idx, old_owner, text = view[idx]
items = now.read_threads()
items[file_idx] = ident.thread_line(text, "most")
gen = ident.log_transfer(text, old_owner, "most", "osha-main")
now.write_threads(items)
v_main2 = {t for _, _, t in now.threads_view("osha-main")}
v_most2 = {t for _, _, t in now.threads_view("most")}
check("7. transfer: владелец сменён, старый агент нить больше не видит",
      "нить главного" not in v_main2 and "нить главного" in v_most2 and gen == 1,
      f"{v_main2=} {v_most2=} {gen=}")

# ── дополнительно: фронтматтер резюме несёт identity_schema ───────────────────
txt = open(os.path.join(now.SESSIONS, f"2026-08-06_{sids['osha-999'][0][:8]}.md"),
           encoding="utf-8").read()
check("8. фронтматтер резюме: identity_schema + agent_id владельца",
      "identity_schema: 1" in txt and "agent_id: osha-999" in txt)

print(f"\nИтог: {len(PASS)} ✅ / {len(FAIL)} 🔴" + (f"  ПРОВАЛЕНО: {FAIL}" if FAIL else ""))
sys.exit(1 if FAIL else 0)
