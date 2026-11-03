"""Runs a chat question in a background thread and records progress in ChatMessage (the portal polls it)."""
from __future__ import annotations

import logging
import threading

from django.db import close_old_connections, connection
from django.utils import timezone

from llmwiki.conf import get_settings
from llmwiki.models import ChatMessage, ChatSession, Email, Page, Report, Thread
from llmwiki.pipeline.templates import section_label
from llmwiki.util import clip_tokens, first_sentence, model_text

log = logging.getLogger(__name__)


def view_context(view: dict) -> str:
    """Text for 'What the user has open' from the portal's view descriptor."""
    kind, vid = (view or {}).get("kind"), (view or {}).get("id")
    try:
        if kind == "page" and vid:
            p = (Page.objects.filter(id=int(vid)).first() if str(vid).isdigit() else None) or Page.objects.filter(slug=vid).first()
            if p:
                secs = "\n\n".join(f"{section_label(p.level, s.key, s.title)}:\n{s.text}" for s in p.sections.all() if s.text.strip())
                return f"Wiki page: {p.title} ({p.level}, status {p.status})\n{secs}"
        if kind == "email" and vid:
            e = Email.objects.filter(id=vid).first()
            if e:
                notes = "\n".join(f"{x.day} {x.from_name or x.from_addr}: {x.note or x.subject}" for x in Email.objects.filter(thread_id=e.thread_id).order_by("sent_at")[:40])
                return f"Email: {e.subject}\nFrom {e.from_name} <{e.from_addr}> on {e.day}\n{model_text(e.new_text)}\n\nThread notes:\n{notes}"
        if kind == "thread" and vid:
            t = Thread.objects.filter(id=vid).first()
            if t:
                notes = "\n".join(f"{x.day} {x.from_name or x.from_addr}: {x.note or x.subject}" for x in Email.objects.filter(thread=t).order_by("sent_at"))
                return f"Thread: {t.subject}\n{notes}"
        if kind == "report" and vid:
            parts = Report.objects.filter(period=vid).order_by("-created_at")[:12]
            return f"Weekly briefing {vid}:\n" + "\n\n".join(r.text for r in parts)
        if view and view.get("label"):
            return f"The user is looking at: {view['label']}" + (f"\n{view.get('detail', '')}" if view.get("detail") else "")
    except Exception:
        log.exception("view context failed")
    return ""


def history_text(session: ChatSession, limit: int) -> str:
    msgs = list(session.messages.filter(status="done").order_by("-created_at")[:limit])
    return "\n".join(f"Q: {m.question}\nA: {first_sentence(m.answer, 300)}" for m in reversed(msgs))


def start(question: str, mode: str, view: dict, session_id: int | None, user: str = "") -> ChatMessage:
    sess = ChatSession.objects.filter(id=session_id).first() if session_id else None
    if not sess:
        sess = ChatSession.objects.create(user=user)
    m = ChatMessage.objects.create(session=sess, question=question.strip()[:4000], mode=mode, view=view or {}, status="queued",
                                   steps=[{"t": timezone.now().isoformat(), "label": "queued"}])
    threading.Thread(target=_run, args=(m.id,), name=f"chat-{m.id}", daemon=True).start()
    return m


def _run(mid: int):
    close_old_connections()
    try:
        m = ChatMessage.objects.get(id=mid)
        m.status = "running"
        m.save(update_fields=["status"])
        s = get_settings()

        def progress(label):
            ChatMessage.objects.filter(id=mid).update(steps=list(ChatMessage.objects.get(id=mid).steps) + [{"t": timezone.now().isoformat(), "label": label}])

        from llmwiki.chat.agent import answer
        view = clip_tokens(view_context(m.view), s.budgets.chat["view"])
        res = answer(m.question, view=view, history=history_text(m.session, s.chat.history_turns), mode=m.mode, progress=progress)
        m.refresh_from_db()
        m.answer = res["answer"]
        m.sources = res["sources"]
        m.confidence = res["confidence"]
        m.steps = list(m.steps) + [{"t": timezone.now().isoformat(), "label": f"done ({res['engine']}, {res['mode']})", "detail": res["steps"]}]
        m.status = "done"
        m.finished_at = timezone.now()
        m.save()
    except Exception as e:
        log.exception("chat %s failed", mid)
        ChatMessage.objects.filter(id=mid).update(status="failed", error=str(e)[:2000], finished_at=timezone.now())
    finally:
        connection.close()
