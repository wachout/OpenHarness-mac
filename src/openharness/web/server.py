"""OpenHarness Web UI — FastAPI HTTP server."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, StreamingResponse
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Web UI requires optional deps. Install with: pip install 'openharness-ai[web]'"
    ) from exc

from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.config.settings import load_settings
from openharness.skills.loader import get_user_skills_dir, load_skill_registry
from openharness.skills.types import SkillDefinition
from openharness.ui.runtime import RuntimeBundle, build_runtime, close_runtime
from openharness.web.db import WebSessionDB

import logging as _logging
_log = _logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="OpenHarness Web UI", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Session store ─────────────────────────────────────────────────────────────

_sessions: dict[str, RuntimeBundle] = {}
_session_meta: dict[str, dict[str, Any]] = {}  # created_at, name, etc.
_db: WebSessionDB | None = None


def _get_db() -> WebSessionDB:
    global _db
    if _db is None:
        _db = WebSessionDB()
    return _db

# ── Skills store ──────────────────────────────────────────────────────────────

_disabled_skills: set[str] = set()  # skill names that are disabled

# ── Pydantic models ───────────────────────────────────────────────────────────


class CreateSessionRequest(BaseModel):
    cwd: str | None = None
    model: str | None = None
    permission_mode: str = "auto"
    name: str | None = None
    active_profile: str | None = None
    api_key: str | None = None
    api_format: str | None = None
    base_url: str | None = None


class ChatRequest(BaseModel):
    prompt: str


class CreateSkillRequest(BaseModel):
    name: str
    description: str = ""
    content: str = ""


# ── Event serializer ──────────────────────────────────────────────────────────


def _serialize_event(event: Any) -> dict[str, Any]:
    if isinstance(event, AssistantTextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, AssistantTurnComplete):
        usage = event.usage
        return {
            "type": "turn_complete",
            "text": event.message.text,
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
            },
        }
    if isinstance(event, ToolExecutionStarted):
        payload: dict[str, Any] = {
            "type": "tool_start",
            "name": event.tool_name,
            "input": event.tool_input,
        }
        # Enrich skill tool with skill name for frontend display
        if event.tool_name == "skill":
            payload["category"] = "skill"
            payload["skill_name"] = str(event.tool_input.get("name") or "")
        # Enrich agent tool with agent metadata for frontend display
        elif event.tool_name == "agent":
            payload["category"] = "agent"
            payload["agent_type"] = str(event.tool_input.get("subagent_type") or "general")
            payload["agent_description"] = str(event.tool_input.get("description") or "")
            payload["agent_mode"] = str(event.tool_input.get("mode") or "local_agent")
        return payload
    if isinstance(event, ToolExecutionCompleted):
        output = event.output
        if len(output) > 800:
            output = output[:800] + "\n… (truncated)"
        payload = {
            "type": "tool_end",
            "name": event.tool_name,
            "output": output,
            "is_error": event.is_error,
        }
        # Parse agent spawn result for frontend display
        if event.tool_name == "agent" and not event.is_error:
            payload["category"] = "agent"
            import re as _re
            m = _re.search(r"Spawned agent (\S+) \(task_id=(\S+?),\s*backend=(\S+?)\)", output)
            if m:
                payload["agent_id"] = m.group(1)
                payload["task_id"] = m.group(2)
                payload["backend_type"] = m.group(3)
        elif event.tool_name == "skill":
            payload["category"] = "skill"
        return payload
    if isinstance(event, ErrorEvent):
        return {"type": "error", "message": event.message, "recoverable": event.recoverable}
    if isinstance(event, StatusEvent):
        return {"type": "status", "message": event.message}
    if isinstance(event, CompactProgressEvent):
        return {"type": "compact", "phase": event.phase, "message": event.message or ""}
    return {"type": "unknown"}


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def serve_index() -> HTMLResponse:
    """Serve the single-page web UI."""
    static_dir = Path(__file__).parent / "static"
    html_file = static_dir / "index.html"
    if not html_file.exists():
        raise HTTPException(status_code=500, detail="index.html not found")
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "status": "ok",
        "session_count": len(_sessions),
        "timestamp": int(time.time()),
    }


@app.get("/api/profiles")
async def api_profiles() -> dict[str, Any]:
    """Return available provider profiles with their configured status."""
    import os

    settings = load_settings()
    profiles_out = []
    active = settings.active_profile or "claude-api"

    _ENV_VARS: dict[str, str] = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "dashscope_api_key": "DASHSCOPE_API_KEY",
        "moonshot_api_key": "MOONSHOT_API_KEY",
        "minimax_api_key": "MINIMAX_API_KEY",
        "gemini_api_key": "GEMINI_API_KEY",
        "deepseek_api_key": "DEEPSEEK_API_KEY",
        "zhipu_api_key": "ZHIPUAI_API_KEY",
        "mistral_api_key": "MISTRAL_API_KEY",
        "groq_api_key": "GROQ_API_KEY",
        "stepfun_api_key": "STEPFUN_API_KEY",
        "baichuan_api_key": "BAICHUAN_API_KEY",
    }

    for profile_key, profile in settings.merged_profiles().items():
        auth_source = profile.auth_source or ""
        env_var = _ENV_VARS.get(auth_source)
        has_key = bool(env_var and os.environ.get(env_var))
        # Also check if it's the active profile with api_key already set in settings
        if not has_key and profile_key == active and settings.api_key:
            has_key = True
        profiles_out.append({
            "id": profile_key,
            "label": profile.label or profile_key,
            "default_model": profile.default_model or "",
            "allowed_models": profile.allowed_models or [],
            "provider": profile.provider or "",
            "configured": has_key,
            "is_active": profile_key == active,
        })

    # Sort: configured first, then active, then by label
    profiles_out.sort(key=lambda p: (not p["configured"], not p["is_active"], p["label"]))

    return {
        "profiles": profiles_out,
        "active_profile": active,
        "active_model": settings.model,
    }


@app.get("/api/fs/ls")
async def fs_ls(path: str = "/") -> dict[str, Any]:
    """List dirs + files under *path* for the directory-picker UI."""
    try:
        target = Path(path).expanduser().resolve()
        if not target.is_dir():
            target = target.parent
        parent = str(target.parent) if target != target.parent else None
        entries = list(target.iterdir())
        dirs = sorted(
            [{"name": e.name, "path": str(e)} for e in entries if e.is_dir() and not e.name.startswith(".")],
            key=lambda d: d["name"].lower(),
        )
        hidden_dirs = sorted(
            [{"name": e.name, "path": str(e)} for e in entries if e.is_dir() and e.name.startswith(".")],
            key=lambda d: d["name"].lower(),
        )
        files = sorted(
            [{"name": e.name, "path": str(e)} for e in entries if e.is_file() and not e.name.startswith(".")],
            key=lambda d: d["name"].lower(),
        )
        hidden_files = sorted(
            [{"name": e.name, "path": str(e)} for e in entries if e.is_file() and e.name.startswith(".")],
            key=lambda d: d["name"].lower(),
        )
        return {
            "path": str(target),
            "parent": parent,
            "dirs": dirs,
            "hidden_dirs": hidden_dirs,
            "files": files,
            "hidden_files": hidden_files,
        }
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/sessions")
async def list_sessions() -> dict[str, Any]:
    result = []
    seen: set[str] = set()
    # Live sessions first
    for sid, bundle in _sessions.items():
        seen.add(sid)
        meta = _session_meta.get(sid, {})
        result.append(
            {
                "session_id": sid,
                "name": meta.get("name") or sid[:8],
                "model": bundle.engine.model,
                "cwd": bundle.cwd,
                "message_count": len(bundle.engine.messages),
                "created_at": meta.get("created_at", 0),
                "live": True,
            }
        )
    # Persisted-only sessions from DB
    db = _get_db()
    for sess in db.list_sessions():
        sid = sess["session_id"]
        if sid in seen:
            continue
        result.append({
            "session_id": sid,
            "name": sess.get("name") or sid[:8],
            "model": sess.get("model", ""),
            "cwd": sess.get("cwd", ""),
            "message_count": db.message_count(sid),
            "created_at": sess.get("created_at", 0),
            "live": False,
        })
    # Sort by created_at desc
    result.sort(key=lambda s: s["created_at"], reverse=True)
    return {"sessions": result}


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    """Create a new agent session."""

    async def _noop_permission_prompt(tool: str, _input: str) -> bool:
        return True  # auto-approve in web mode

    async def _noop_ask_user(question: str) -> str:
        return ""  # no interactive prompt in web mode

    bundle = await build_runtime(
        cwd=req.cwd,
        model=req.model,
        permission_mode=req.permission_mode,
        active_profile=req.active_profile,
        api_key=req.api_key,
        api_format=req.api_format,
        base_url=req.base_url,
        permission_prompt=_noop_permission_prompt,
        ask_user_prompt=_noop_ask_user,
        enforce_max_turns=False,
    )
    sid = bundle.session_id
    _sessions[sid] = bundle
    created = int(time.time())
    sess_name = req.name or sid[:8]
    _session_meta[sid] = {
        "created_at": created,
        "name": sess_name,
    }
    # Persist to SQLite
    _get_db().save_session(
        session_id=sid,
        name=sess_name,
        cwd=bundle.cwd,
        model=bundle.engine.model,
        active_profile=req.active_profile or "",
        permission_mode=req.permission_mode or "auto",
        api_key=req.api_key or "",
        api_format=req.api_format or "",
        base_url=req.base_url or "",
        created_at=created,
    )
    return {
        "session_id": sid,
        "model": bundle.engine.model,
        "cwd": bundle.cwd,
        "name": sess_name,
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    if session_id in _sessions:
        bundle = _sessions.pop(session_id)
        await close_runtime(bundle)
    _session_meta.pop(session_id, None)
    _get_db().delete_session(session_id)
    return {"deleted": session_id}


@app.get("/api/sessions/{session_id}/history")
async def session_history(session_id: str) -> dict[str, Any]:
    # Live session — return engine messages
    if session_id in _sessions:
        bundle = _sessions[session_id]
        messages = [
            {"role": msg.role, "text": msg.text}
            for msg in bundle.engine.messages
            if msg.text.strip()
        ]
        return {
            "session_id": session_id,
            "messages": messages,
            "model": bundle.engine.model,
        }
    # Persisted session — load from DB
    db = _get_db()
    sess = db.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    db_msgs = db.get_messages(session_id)
    messages = [{"role": m["role"], "text": m["content"]} for m in db_msgs]
    return {
        "session_id": session_id,
        "messages": messages,
        "model": sess.get("model", ""),
    }


@app.post("/api/chat/{session_id}")
async def chat(session_id: str, req: ChatRequest) -> StreamingResponse:
    """Submit a prompt and stream back SSE events."""
    if session_id not in _sessions:
        # Try to restore session from DB
        restored = await _try_restore_session(session_id)
        if not restored:
            raise HTTPException(status_code=404, detail="Session not found")
    bundle = _sessions[session_id]
    db = _get_db()

    # Persist user message
    db.add_message(session_id=session_id, role="user", content=req.prompt)

    assistant_text_parts: list[str] = []

    async def event_stream():
        try:
            async for event in bundle.engine.submit_message(req.prompt):
                serialized = _serialize_event(event)
                data = json.dumps(serialized, ensure_ascii=False)
                yield f"data: {data}\n\n"
                # Collect assistant text
                if isinstance(event, AssistantTurnComplete):
                    assistant_text_parts.append(event.message.text)
        except Exception as exc:  # noqa: BLE001
            err = json.dumps({"type": "error", "message": str(exc), "recoverable": False})
            yield f"data: {err}\n\n"
        finally:
            yield "data: [DONE]\n\n"
            # Persist assistant reply
            full_text = "".join(assistant_text_parts)
            if full_text.strip():
                db.add_message(session_id=session_id, role="assistant", content=full_text)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Skills routes ─────────────────────────────────────────────────────────────


@app.get("/api/skills")
async def list_skills() -> dict[str, Any]:
    """List all loaded skills with their enabled/disabled status."""
    registry = load_skill_registry()
    skills_out = []
    for skill in registry.list_skills():
        skills_out.append({
            "name": skill.name,
            "description": skill.description,
            "source": skill.source,
            "path": skill.path or "",
            "enabled": skill.name not in _disabled_skills,
        })
    return {"skills": skills_out}


@app.post("/api/skills")
async def create_skill(req: CreateSkillRequest) -> dict[str, Any]:
    """Create a new user skill."""
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Skill name is required")
    # sanitise folder name
    folder_name = name.lower().replace(" ", "-")
    user_dir = get_user_skills_dir()
    skill_dir = user_dir / folder_name
    if skill_dir.exists():
        raise HTTPException(status_code=409, detail=f"Skill '{name}' already exists")
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = f"---\nname: {name}\ndescription: {req.description}\n---\n\n# {name}\n\n{req.content}\n"
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")
    return {"created": name, "path": str(skill_dir / "SKILL.md")}


@app.delete("/api/skills/{skill_name}")
async def delete_skill(skill_name: str) -> dict[str, Any]:
    """Delete a user skill by name."""
    import shutil

    registry = load_skill_registry()
    skill = registry.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.source != "user":
        raise HTTPException(status_code=403, detail="Only user skills can be deleted")
    if skill.path:
        skill_dir = Path(skill.path).parent
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
    _disabled_skills.discard(skill_name)
    return {"deleted": skill_name}


@app.put("/api/skills/{skill_name}/toggle")
async def toggle_skill(skill_name: str) -> dict[str, Any]:
    """Toggle a skill between enabled and disabled."""
    registry = load_skill_registry()
    skill = registry.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill_name in _disabled_skills:
        _disabled_skills.discard(skill_name)
        enabled = True
    else:
        _disabled_skills.add(skill_name)
        enabled = False
    return {"name": skill_name, "enabled": enabled}


# ── Remote Skills routes ─────────────────────────────────────────────────────


class InstallSkillRequest(BaseModel):
    source: str  # "anthropics" | "skills_sh"
    name: str
    url: str = ""
    repo_path: str = ""
    description: str = ""


@app.get("/api/skills/remote")
async def list_remote_skills(source: str = "all") -> dict[str, Any]:
    """Fetch available skills from remote sources."""
    import asyncio
    from openharness.services.skill_fetcher import fetch_github_skills, fetch_skills_sh

    all_skills: list[dict[str, Any]] = []
    errors: list[str] = []

    # Check which skills are already installed
    registry = load_skill_registry()
    installed_names = {s.name.lower() for s in registry.list_skills()}

    # Fetch from sources concurrently with overall timeout
    tasks = []
    source_names = []
    if source in ("all", "anthropics"):
        tasks.append(fetch_github_skills())
        source_names.append("anthropics")
    if source in ("all", "skills_sh"):
        tasks.append(fetch_skills_sh())
        source_names.append("skills_sh")

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=45.0,  # Overall timeout for all sources
        )
    except asyncio.TimeoutError:
        errors.append("Request timed out (45s) - remote sources may be unavailable")
        results = []

    for idx, result in enumerate(results):
        src_name = source_names[idx] if idx < len(source_names) else "unknown"
        if isinstance(result, Exception):
            errors.append(f"{src_name}: {result}")
            continue
        if result.error:
            errors.append(f"{src_name}: {result.error}")
        for s in result.skills:
            all_skills.append({
                "name": s.name,
                "description": s.description,
                "source": s.source,
                "url": s.url,
                "repo_path": s.repo_path,
                "installed": s.name.lower() in installed_names,
            })

    return {"skills": all_skills, "total": len(all_skills), "errors": errors}


@app.get("/api/skills/remote/sources")
async def list_remote_sources() -> dict[str, Any]:
    """List available remote skill sources."""
    return {
        "sources": [
            {
                "id": "anthropics",
                "name": "Anthropic Skills",
                "url": "https://github.com/anthropics/skills",
                "description": "Official skills from Anthropic's GitHub repository",
            },
            {
                "id": "skills_sh",
                "name": "skills.sh",
                "url": "https://skills.sh/",
                "description": "Community skills registry at skills.sh",
            },
        ]
    }


@app.post("/api/skills/remote/install")
async def install_remote_skill_endpoint(req: InstallSkillRequest) -> dict[str, Any]:
    """Install a remote skill to the user skills directory."""
    from openharness.services.skill_fetcher import RemoteSkillInfo, install_remote_skill

    skill_info = RemoteSkillInfo(
        name=req.name,
        description=req.description,
        source=req.source,
        url=req.url,
        repo_path=req.repo_path,
    )
    dest = get_user_skills_dir()
    result = await install_remote_skill(skill_info, dest)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ── Dev entry point ───────────────────────────────────────────────────────────


async def _try_restore_session(session_id: str) -> bool:
    """Attempt to rebuild a RuntimeBundle from DB-persisted session config."""
    db = _get_db()
    sess = db.get_session(session_id)
    if not sess:
        return False
    try:
        async def _noop_permission_prompt(tool: str, _input: str) -> bool:
            return True

        async def _noop_ask_user(question: str) -> str:
            return ""

        bundle = await build_runtime(
            cwd=sess.get("cwd") or None,
            model=sess.get("model") or None,
            permission_mode=sess.get("permission_mode") or "auto",
            active_profile=sess.get("active_profile") or None,
            api_key=sess.get("api_key") or None,
            api_format=sess.get("api_format") or None,
            base_url=sess.get("base_url") or None,
            permission_prompt=_noop_permission_prompt,
            ask_user_prompt=_noop_ask_user,
            enforce_max_turns=False,
        )
        # Restore chat history into engine's internal _messages list
        from openharness.engine.messages import ConversationMessage, TextBlock
        for msg in db.get_messages(session_id):
            cm = ConversationMessage(
                role=msg["role"],
                content=[TextBlock(text=msg["content"])],
            )
            bundle.engine._messages.append(cm)
        # Patch session_id to match original
        bundle.session_id = session_id
        _sessions[session_id] = bundle
        _session_meta[session_id] = {
            "created_at": sess.get("created_at", 0),
            "name": sess.get("name") or session_id[:8],
        }
        _log.info("Restored session %s from DB", session_id[:8])
        return True
    except Exception:
        _log.warning("Failed to restore session %s", session_id[:8], exc_info=True)
        return False


def run_server(host: str = "127.0.0.1", port: int = 7860, reload: bool = False) -> None:
    """Start the uvicorn server (called from CLI)."""
    try:
        import uvicorn
    except ImportError as exc:
        raise ImportError(
            "uvicorn not installed. Run: pip install 'openharness-ai[web]'"
        ) from exc

    uvicorn.run(
        "openharness.web.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
