# -*- coding: utf-8 -*-
"""Идентичность агентов — ЕДИНСТВЕННЫЙ адаптер (вердикт архитектора 06.08.2026).

Баг «винегрет сессий»: три Claude Code (osha-main, osha-999, most) с общей
памятью после обрыва цепляли чужие сессии. Модель разграничения:

    profile_id  — какой профиль/настройки (osha)
    agent_id    — КТО владеет памятью и нитями, живёт долго (osha-999)
    instance_id — конкретный запуск процесса (UUID), меняется после смерти
    session_id  — конкретная сессия Claude Code

🔴 agent_id ≠ instance_id: 999 — не «второй процесс osha», а отдельный
долговечный владелец continuity. Источник истины о существовании агентов —
identity/agents.yaml; о запусках и принадлежности сессий — registry.sqlite.
Launcher (agent_launch.py) НАЗНАЧАЕТ идентичность; env var только переносит
её внутрь хуков; хуки проверяют, но не создают.

Старые сессии (до разграничения) НЕ размечаются эвристикой: владелец
неизвестен (legacy-unowned). Ручная разметка — только через append-only
session_claims с доказательством происхождения.

Через этот модуль работают: SessionStart/End (hook.py), summarizer,
auto_recall2, memoryctl search, нити (now.py). Сам поиск/эмбеддинги/стор
не трогаются — только фильтрация и подпись их результатов.
"""

import json
import os
import re
import sqlite3
import time
import uuid

IDENTITY_SCHEMA = 1
LEGACY = "legacy-unowned"

ID_DIR = os.environ.get("CONTINUITY_IDENTITY_DIR") or os.path.join(
    os.path.expanduser("~/.claude/continuity"), "identity")
AGENTS_YAML = os.path.join(ID_DIR, "agents.yaml")
DB_PATH = os.path.join(ID_DIR, "registry.sqlite")

ENV_AGENT = "AGENT_ID"
ENV_PROFILE = "AGENT_PROFILE"
ENV_INSTANCE = "AGENT_INSTANCE_ID"
ENV_GENERATION = "AGENT_GENERATION"

# Метка владельца в строке нити THREADS.md: «- [agent:osha-999] текст».
# Строки без метки — legacy: заведены до разграничения, владелец неизвестен,
# видны всем (обязательства перед Рувимом не должны исчезнуть из виду).
THREAD_TAG = re.compile(r"^\[agent:([A-Za-z0-9_-]+)\]\s*")


# ── статический реестр ────────────────────────────────────────────────────────

def load_agents(path=None):
    """agents.yaml → {agent_id: {"profile": ...}}.

    Разбор свой, без pyyaml: формат файла жёстко ограничен (две колонки
    отступов), а лишняя зависимость в хуке — лишний способ сломаться.
    """
    path = path or AGENTS_YAML
    agents, cur = {}, None
    try:
        with open(path, encoding="utf-8") as f:
            in_agents = False
            for line in f:
                line = line.rstrip()
                if not line or line.lstrip().startswith("#"):
                    continue
                if line == "agents:":
                    in_agents = True
                    continue
                if not in_agents:
                    continue
                m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
                if m:
                    cur = m.group(1)
                    agents[cur] = {}
                    continue
                m = re.match(r"^    (\w+):\s*(\S+)\s*$", line)
                if m and cur:
                    agents[cur][m.group(1)] = m.group(2)
    except OSError:
        pass
    return agents


def known_agent(agent_id):
    return agent_id in load_agents()


# ── runtime-реестр (SQLite) ───────────────────────────────────────────────────

def db(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("""CREATE TABLE IF NOT EXISTS agents(
        agent_id TEXT PRIMARY KEY, profile_id TEXT, enabled INTEGER DEFAULT 1)""")
    con.execute("""CREATE TABLE IF NOT EXISTS instances(
        instance_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
        generation INTEGER NOT NULL, pid INTEGER,
        started_at TEXT NOT NULL, ended_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS sessions(
        session_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
        instance_id TEXT, transcript_path TEXT,
        started_at TEXT NOT NULL, ended_at TEXT)""")
    # Ручная разметка старых сессий: ТОЛЬКО append-only, с доказательством.
    # Транскрипт и прежние записи не переписываются никогда.
    con.execute("""CREATE TABLE IF NOT EXISTS session_claims(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        claimed_by TEXT NOT NULL, evidence TEXT NOT NULL,
        claimed_at TEXT NOT NULL)""")
    # Передача нити другому агенту — атомарная операция со сменой владельца
    # и generation++; журнал append-only (нить сама живёт в THREADS.md).
    con.execute("""CREATE TABLE IF NOT EXISTS thread_transfers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_text TEXT NOT NULL, from_agent TEXT, to_agent TEXT NOT NULL,
        by_agent TEXT, generation INTEGER NOT NULL, at TEXT NOT NULL)""")
    # Статический реестр зеркалируется в таблицу (для join'ов и Control Plane);
    # источник истины — agents.yaml.
    for aid, meta in load_agents().items():
        con.execute("INSERT OR IGNORE INTO agents(agent_id, profile_id) VALUES(?,?)",
                    (aid, meta.get("profile") or ""))
    con.commit()
    return con


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── текущая идентичность (из env, назначает launcher) ─────────────────────────

def current():
    """Идентичность текущего процесса или None (запуск мимо launcher).

    Env только ПЕРЕНОСИТ назначенное: неизвестный agents.yaml agent_id
    отвергается — координатор проверяет, но не создаёт.
    """
    aid = (os.environ.get(ENV_AGENT) or "").strip()
    if not aid:
        return None
    agents = load_agents()
    if aid not in agents:
        return None
    return {
        "agent_id": aid,
        "profile_id": os.environ.get(ENV_PROFILE) or agents[aid].get("profile") or "",
        "instance_id": os.environ.get(ENV_INSTANCE) or "",
        "generation": os.environ.get(ENV_GENERATION) or "",
    }


def current_agent_id():
    c = current()
    return c["agent_id"] if c else None


# ── запуски (для launcher) ────────────────────────────────────────────────────

def register_instance(agent_id, pid=None, con=None):
    """Новый запуск агента. Возвращает (instance_id, generation)."""
    if not known_agent(agent_id):
        raise ValueError(f"агент {agent_id!r} не описан в {AGENTS_YAML}")
    con = con or db()
    gen = (con.execute("SELECT COALESCE(MAX(generation),0) FROM instances "
                       "WHERE agent_id=?", (agent_id,)).fetchone()[0] or 0) + 1
    iid = str(uuid.uuid4())
    con.execute("INSERT INTO instances(instance_id, agent_id, generation, pid, started_at)"
                " VALUES(?,?,?,?,?)", (iid, agent_id, gen, pid, _now()))
    con.commit()
    return iid, gen


def end_instance(instance_id, con=None):
    con = con or db()
    con.execute("UPDATE instances SET ended_at=? WHERE instance_id=? AND ended_at IS NULL",
                (_now(), instance_id))
    con.commit()


# ── принадлежность сессий ─────────────────────────────────────────────────────

def bind_session(session_id, transcript_path=None, con=None):
    """Write-once binding: session_id → agent_id + instance_id текущего процесса.

    Возвращает статус:
      "bound"     — привязана сейчас;
      "already"   — уже привязана к НАМ (resume своей сессии);
      "conflict:<owner>" — сессия принадлежит другому агенту. Это НЕ overwrite,
                    а жёсткая ошибка: кто-то resume'ит чужую сессию. Привязка
                    в реестре сохраняется прежней.
      "anonymous" — у процесса нет назначенной идентичности (запуск мимо
                    launcher); сессия остаётся неразмеченной.
    """
    me = current()
    if not session_id:
        return "anonymous"
    if not me:
        return "anonymous"
    con = con or db()
    row = con.execute("SELECT agent_id FROM sessions WHERE session_id=?",
                      (session_id,)).fetchone()
    if row:
        return "already" if row[0] == me["agent_id"] else f"conflict:{row[0]}"
    con.execute("INSERT INTO sessions(session_id, agent_id, instance_id, transcript_path,"
                " started_at) VALUES(?,?,?,?,?)",
                (session_id, me["agent_id"], me["instance_id"] or None,
                 transcript_path, _now()))
    con.commit()
    return "bound"


def end_session(session_id, con=None):
    if not session_id:
        return
    con = con or db()
    con.execute("UPDATE sessions SET ended_at=? WHERE session_id=? AND ended_at IS NULL",
                (_now(), session_id))
    con.commit()


def owner_of(session_id, con=None):
    """Владелец сессии: agent_id, либо None = legacy-unowned.

    Привязка launcher'а (sessions) сильнее ручной разметки; из claims берётся
    ПОСЛЕДНЯЯ запись (журнал append-only, поздняя правка не стирает раннюю).
    Принимает и полный id, и префикс ≥8 знаков (эпизоды хранят 8).
    """
    if not session_id:
        return None
    con = con or db()
    sid = str(session_id)
    if len(sid) >= 32:
        row = con.execute("SELECT agent_id FROM sessions WHERE session_id=?",
                          (sid,)).fetchone()
    else:
        row = con.execute("SELECT agent_id FROM sessions WHERE session_id LIKE ?",
                          (sid[:8] + "%",)).fetchone()
    if row:
        return row[0]
    row = con.execute(
        "SELECT agent_id FROM session_claims WHERE session_id LIKE ? "
        "ORDER BY id DESC LIMIT 1", (sid[:8] + "%",)).fetchone()
    return row[0] if row else None


def owner_map(con=None):
    """{префикс8 сессии: agent_id} — для фильтрации пачки находок без N запросов."""
    con = con or db()
    out = {}
    for sid, aid in con.execute("SELECT session_id, agent_id FROM session_claims ORDER BY id"):
        out[sid[:8]] = aid          # ранние claims ниже — поздние перекроют
    for sid, aid in con.execute("SELECT session_id, agent_id FROM sessions"):
        out[sid[:8]] = aid          # привязка launcher'а сильнее claims
    return out


def claim_session(session_id, agent_id, claimed_by, evidence, con=None):
    """Ручная разметка старой сессии. Требует agent_id из реестра и доказательство.

    «По стилю похоже» доказательством НЕ является — только запись launcher'а,
    уникальный binding, авторитетный журнал (вердикт архитектора, пункт в).
    """
    if not known_agent(agent_id):
        raise ValueError(f"агент {agent_id!r} не описан в agents.yaml")
    if not (evidence or "").strip():
        raise ValueError("claim без доказательства запрещён")
    con = con or db()
    con.execute("INSERT INTO session_claims(session_id, agent_id, claimed_by, evidence,"
                " claimed_at) VALUES(?,?,?,?,?)",
                (session_id, agent_id, claimed_by or "?", evidence, _now()))
    con.commit()


# ── фильтрация находок поиска ─────────────────────────────────────────────────

def classify(session_prefix, me=None, omap=None):
    """'mine' | 'foreign' | 'legacy' | 'global' для находки поиска."""
    if not session_prefix:
        return "global"             # память/факты/артефакты — общий канон
    omap = omap if omap is not None else owner_map()
    owner = omap.get(str(session_prefix)[:8])
    if owner is None:
        return "legacy"
    me = me if me is not None else current_agent_id()
    return "mine" if owner == me else "foreign"


def apply_scope(hits, scope="mine", agent=None, include_legacy=True, me=None):
    """Фильтр находок по принадлежности сессий. Сами находки не пересобираются.

    scope="mine" (умолчание): свои + общий канон (+ legacy с пометкой
      «владелец неизвестен», если include_legacy). Чужие агенты — отсекаются:
      видимость ≠ автоматическая инъекция.
    scope="all": всё; каждая чужая находка обязана нести автора.
    agent="most": только сессии этого агента (+ общий канон) — явный
      cross-agent запрос «что делал most».

    Возвращает (оставленные, число отсечённых чужих). Каждой находке
    проставляются h["owner"] и, для показа, h["owner_note"].
    """
    omap = owner_map()
    me = me if me is not None else current_agent_id()
    kept, dropped = [], 0
    for h in hits:
        sp = (h.get("session") or "")[:8]
        cls = classify(sp, me=me, omap=omap)
        owner = omap.get(sp) if sp else None
        h["owner"] = owner if cls != "legacy" else LEGACY
        note = None
        if cls == "legacy":
            h["owner"] = LEGACY
            note = "[владелец сессии неизвестен]"
        elif cls == "foreign":
            note = f"[автор: {owner}]"
        elif cls == "mine" and scope == "all" and owner:
            note = f"[автор: {owner}]"

        if agent:                                   # явный запрос про агента X
            if cls == "global" or owner == agent:
                h["owner_note"] = note
                kept.append(h)
            elif cls in ("mine", "foreign", "legacy"):
                dropped += 1
            continue
        if scope == "all":
            h["owner_note"] = note
            kept.append(h)
            continue
        # scope == "mine"
        if cls == "foreign":
            dropped += 1
            continue
        if cls == "legacy" and not include_legacy:
            dropped += 1
            continue
        h["owner_note"] = note
        kept.append(h)
    return kept, dropped


# ── нити (THREADS.md, метки владельца в строках) ──────────────────────────────

def thread_owner(text):
    """Строка нити → (owner|None, чистый текст)."""
    m = THREAD_TAG.match(text)
    if m:
        return m.group(1), text[m.end():]
    return None, text


def thread_line(text, owner):
    return f"[agent:{owner}] {text}" if owner else text


def thread_visible(owner, me):
    """Видна ли нить агенту me: своя или legacy (без метки).

    Legacy-нити видны всем: это живые обязательства перед Рувимом, заведённые
    до разграничения — исчезнуть из виду они не должны. Чужие (с меткой
    другого агента) не видны.
    """
    return owner is None or owner == me


def log_transfer(thread_text, from_agent, to_agent, by_agent, con=None):
    con = con or db()
    gen = (con.execute("SELECT COALESCE(MAX(generation),0) FROM thread_transfers "
                       "WHERE thread_text=?", (thread_text,)).fetchone()[0] or 0) + 1
    con.execute("INSERT INTO thread_transfers(thread_text, from_agent, to_agent,"
                " by_agent, generation, at) VALUES(?,?,?,?,?,?)",
                (thread_text, from_agent, to_agent, by_agent, gen, _now()))
    con.commit()
    return gen


# ── frontmatter резюме сессии ─────────────────────────────────────────────────

def summary_identity_fm(session_id, con=None):
    """Строки фронтматтера идентичности для резюме сессии.

    Владелец берётся из ПРИВЯЗКИ СЕССИИ, а не из env суммаризатора: резюме
    может строить чужой воркер при догоне. Существующее поле `agent: claude`
    не переиспользуется — оно означает другое (claude/codex) и остаётся.
    """
    con = con or db()
    row = con.execute("SELECT agent_id, instance_id FROM sessions WHERE session_id=?",
                      (session_id,)).fetchone()
    owner = row[0] if row else (owner_of(session_id, con=con) or LEGACY)
    iid = row[1] if row else None
    agents = load_agents()
    profile = agents.get(owner, {}).get("profile", "") if owner != LEGACY else ""
    lines = [f"identity_schema: {IDENTITY_SCHEMA}",
             f"agent_id: {owner}"]
    if profile:
        lines.append(f"profile_id: {profile}")
    if iid:
        lines.append(f"instance_id: {iid}")
    return lines


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Реестр идентичности агентов")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("whoami")
    sub.add_parser("agents")
    p = sub.add_parser("instances")
    p.add_argument("--agent")
    p = sub.add_parser("sessions")
    p.add_argument("--agent")
    p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("claim", help="ручная разметка старой сессии (append-only)")
    p.add_argument("session_id")
    p.add_argument("agent_id")
    p.add_argument("--evidence", required=True,
                   help="доказательство происхождения (не «по стилю похоже»)")
    a = ap.parse_args()

    if a.cmd == "whoami":
        c = current()
        print(json.dumps(c or {"agent_id": None,
                               "note": "идентичность не назначена (запуск мимо launcher)"},
                         ensure_ascii=False, indent=1))
    elif a.cmd == "agents":
        con = db()
        for aid, prof in con.execute("SELECT agent_id, profile_id FROM agents ORDER BY 1"):
            n = con.execute("SELECT COUNT(*) FROM sessions WHERE agent_id=?", (aid,)).fetchone()[0]
            i = con.execute("SELECT COUNT(*) FROM instances WHERE agent_id=?", (aid,)).fetchone()[0]
            print(f"{aid:12} профиль={prof:6} сессий={n:4} запусков={i}")
    elif a.cmd == "instances":
        con = db()
        q = "SELECT instance_id, agent_id, generation, pid, started_at, ended_at FROM instances"
        args = ()
        if a.agent:
            q += " WHERE agent_id=?"
            args = (a.agent,)
        for r in con.execute(q + " ORDER BY started_at DESC LIMIT 30", args):
            print(f"{r[1]:12} gen={r[2]:<3} pid={r[3] or '?':<7} {r[4]} → {r[5] or 'жив'}  {r[0][:8]}")
    elif a.cmd == "sessions":
        con = db()
        q = "SELECT session_id, agent_id, started_at, ended_at FROM sessions"
        args = ()
        if a.agent:
            q += " WHERE agent_id=?"
            args = (a.agent,)
        for r in con.execute(q + " ORDER BY started_at DESC LIMIT ?", args + (a.limit,)):
            print(f"{r[1]:12} {r[2]} → {r[3] or 'идёт':19} {r[0]}")
    elif a.cmd == "claim":
        claim_session(a.session_id, a.agent_id,
                      current_agent_id() or "manual", a.evidence)
        print(f"claim записан: {a.session_id[:8]} → {a.agent_id} (append-only)")
    else:
        ap.print_help()


if __name__ == "__main__":
    _cli()
