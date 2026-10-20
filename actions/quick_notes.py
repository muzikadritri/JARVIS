"""
actions/quick_notes.py — notes taken by voice, kept on this machine.

Memory is for what JARVIS should know about the user. Notes are for what the
user wants to keep: an idea in the middle of a meeting, a shopping list, the
name of a library somebody mentioned. They are stored as plain JSON in
~/.jarvis/notes.json (written atomically, so a crash mid-save never loses the
file) and exportable as Markdown to the desktop.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from core import outcome

NOTES_PATH = Path.home() / ".jarvis" / "notes.json"
EXPORT_DIR = Path.home() / "Desktop"
MAX_NOTE_CHARS = 4000
_lock = threading.Lock()


def _load() -> list[dict]:
    try:
        data = json.loads(NOTES_PATH.read_text(encoding="utf-8"))
        return [n for n in data if isinstance(n, dict)] if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[Notes] notes file unreadable, starting empty view: {e}")
        return []


def _save(notes: list[dict]) -> None:
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = NOTES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(NOTES_PATH)


def _tags(text: str, extra: str) -> list[str]:
    found = {t.lower() for t in re.findall(r"#([\w-]{2,30})", text)}
    found |= {t.strip().lower().lstrip("#") for t in str(extra or "").split(",") if t.strip()}
    return sorted(found)


def _show(player, title: str, notes: list[dict]) -> None:
    if not player or not hasattr(player, "show_content"):
        return
    lines = [f"#{n['id']}  {n['text']}" + (f"  [{', '.join(n.get('tags') or [])}]" if n.get("tags") else "")
             for n in notes]
    try:
        player.show_content(title, "\n".join(lines) or "No notes.")
    except Exception:
        pass


def _matches(note: dict, query: str) -> bool:
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
    hay = (note.get("text", "") + " " + " ".join(note.get("tags") or [])).lower()
    return all(w in hay for w in words) if words else False


def quick_notes(parameters: dict, player=None, **_ignored) -> str:
    params = parameters or {}
    action = str(params.get("action") or "list").strip().lower()
    with _lock:
        notes = _load()
        if action in ("add", "save", "write"):
            text = str(params.get("text") or "").strip()
            if not text:
                return outcome.ask("quick_notes", "What should the note say?")
            next_id = max((int(n.get("id", 0)) for n in notes), default=0) + 1
            note = {"id": next_id, "text": text[:MAX_NOTE_CHARS],
                    "tags": _tags(text, params.get("tags") or ""),
                    "created": datetime.now().isoformat(timespec="seconds")}
            notes.append(note)
            _save(notes)
            return outcome.done("quick_notes", saved=next_id, total=len(notes),
                                tags=",".join(note["tags"]) or None)

        if action in ("list", "show", "recent"):
            limit = max(1, min(50, int(params.get("limit") or 10)))
            recent = notes[-limit:][::-1]
            _show(player, "NOTES", recent)
            return outcome.done("quick_notes", total=len(notes), shown=len(recent)) + "\n" + \
                outcome.untrusted("notes", "\n".join(f"#{n['id']}: {n['text']}" for n in recent))

        if action in ("search", "find"):
            query = str(params.get("query") or params.get("text") or "").strip()
            if not query:
                return outcome.ask("quick_notes", "What should be searched for?")
            hits = [n for n in notes if _matches(n, query)][::-1][:20]
            _show(player, f"NOTES — {query[:30]}", hits)
            return outcome.done("quick_notes", query=query, found=len(hits)) + "\n" + \
                outcome.untrusted("notes", "\n".join(f"#{n['id']}: {n['text']}" for n in hits))

        if action in ("delete", "remove"):
            target = str(params.get("note_id") or "").strip().lstrip("#")
            if target.lower() in ("last", "latest") and notes:
                target = str(notes[-1]["id"])
            if not target.isdigit():
                return outcome.ask("quick_notes", "Which note number should be deleted?")
            kept = [n for n in notes if str(n.get("id")) != target]
            if len(kept) == len(notes):
                return outcome.failed("quick_notes", f"there is no note #{target}")
            _save(kept)
            return outcome.done("quick_notes", deleted=int(target), total=len(kept))

        if action == "clear":
            if str(params.get("confirm") or "").lower() not in ("yes", "true", "1"):
                return outcome.ask("quick_notes", f"Delete all {len(notes)} notes? This "
                                   "cannot be undone — confirm with confirm=yes.")
            backup = NOTES_PATH.with_name(f"notes-backup-{int(time.time())}.json")
            if notes:
                backup.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")
            _save([])
            return outcome.done("quick_notes", cleared=len(notes), backup=str(backup) if notes else None)

        if action == "export":
            if not notes:
                return outcome.failed("quick_notes", "there are no notes to export")
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            path = EXPORT_DIR / f"JARVIS-Notes-{datetime.now():%Y-%m-%d}.md"
            body = ["# JARVIS Notes", ""]
            for n in notes:
                tag_text = " ".join(f"#{t}" for t in n.get("tags") or [])
                body.append(f"- **{n.get('created', '')[:16].replace('T', ' ')}** — {n['text']} {tag_text}".rstrip())
            path.write_text("\n".join(body) + "\n", encoding="utf-8")
            return outcome.done("quick_notes", exported=str(path), notes=len(notes))

    return outcome.failed("quick_notes", f"unknown action '{action}'",
                          valid="add,list,search,delete,clear,export")


TOOL = {
    "name": "quick_notes",
    "description": (
        "The user's own notes, kept on this computer: 'add' a note (hashtags "
        "become tags), 'list' recent notes, 'search', 'delete' by number or "
        "'last', 'export' all notes as Markdown to the desktop, 'clear' (asks "
        "first). Use when the user says 'note that', 'write down', 'remember to' "
        "for something THEY want to keep — save_memory is for facts about them."
    ),
    "brief": "User notes: add, list, search, delete, export, clear.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "add | list | search | delete | export | clear"},
            "text": {"type": "STRING", "description": "add: the note"},
            "tags": {"type": "STRING", "description": "add: optional comma-separated tags"},
            "query": {"type": "STRING", "description": "search: words to find"},
            "note_id": {"type": "STRING", "description": "delete: note number or 'last'"},
            "limit": {"type": "INTEGER", "description": "list: how many (default 10)"},
            "confirm": {"type": "STRING", "description": "clear: 'yes' once the user confirmed"},
        },
        "required": ["action"],
    },
    "handler": quick_notes,
}
