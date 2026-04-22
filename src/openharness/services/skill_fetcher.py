"""Remote skills fetcher — download skills from GitHub and skills.sh."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

GITHUB_ANTHROPICS_SKILLS = "https://github.com/anthropics/skills"
SKILLS_SH_URL = "https://skills.sh/"

# GitHub raw content patterns
_GITHUB_API_BASE = "https://api.github.com"
_GITHUB_RAW_BASE = "https://raw.githubusercontent.com"

# Timeout for HTTP requests
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Max concurrent requests when fetching skill metadata
_MAX_CONCURRENT_FETCHES = 10


@dataclass
class RemoteSkillInfo:
    """Metadata for a remotely available skill."""

    name: str
    description: str
    source: str  # "anthropics" | "skills_sh"
    url: str  # direct download URL
    repo_path: str  # path within repository
    installed: bool = False


@dataclass
class FetchResult:
    """Result of a remote skills fetch operation."""

    skills: list[RemoteSkillInfo] = field(default_factory=list)
    error: str | None = None


# ── GitHub fetcher ───────────────────────────────────────────────────────────


async def fetch_github_skills(
    repo: str = "anthropics/skills",
    *,
    subdirectory: str | None = None,
) -> FetchResult:
    """Fetch the list of available skills from a GitHub repository.

    Scans for SKILL.md files in the repository tree.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # Use GitHub Trees API for efficient recursive listing
            url = f"{_GITHUB_API_BASE}/repos/{repo}/git/trees/main?recursive=1"
            resp = await client.get(url)
            if resp.status_code == 404:
                # Try 'master' branch
                url = f"{_GITHUB_API_BASE}/repos/{repo}/git/trees/master?recursive=1"
                resp = await client.get(url)
            if resp.status_code != 200:
                return FetchResult(error=f"GitHub API error: {resp.status_code}")

            data = resp.json()
            tree = data.get("tree", [])

            skills: list[RemoteSkillInfo] = []
            skill_paths: list[str] = []

            for item in tree:
                path = item.get("path", "")
                if item.get("type") != "blob":
                    continue
                # Match SKILL.md files or top-level .md files in skills dirs
                if path.endswith("/SKILL.md") or (
                    path.endswith(".md") and "/" in path and _is_skill_dir(path, tree)
                ):
                    if subdirectory and not path.startswith(subdirectory):
                        continue
                    skill_paths.append(path)

            # Fetch metadata for each skill concurrently with semaphore
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)

            async def _fetch_one(spath: str) -> RemoteSkillInfo | None:
                async with semaphore:
                    raw_url = f"{_GITHUB_RAW_BASE}/{repo}/main/{spath}"
                    try:
                        r = await client.get(raw_url)
                        if r.status_code == 404:
                            raw_url = f"{_GITHUB_RAW_BASE}/{repo}/master/{spath}"
                            r = await client.get(raw_url)
                        if r.status_code == 200:
                            content = r.text
                            name, description = _parse_remote_skill(spath, content)
                            return RemoteSkillInfo(
                                name=name,
                                description=description,
                                source="anthropics",
                                url=raw_url,
                                repo_path=spath,
                            )
                    except httpx.HTTPError:
                        logger.debug("Failed to fetch skill at %s", spath)
                    return None

            results = await asyncio.gather(
                *[_fetch_one(sp) for sp in skill_paths],
                return_exceptions=True,
            )
            for res in results:
                if isinstance(res, RemoteSkillInfo):
                    skills.append(res)

            return FetchResult(skills=skills)

    except httpx.HTTPError as e:
        return FetchResult(error=f"Network error: {e}")
    except Exception as e:
        logger.exception("Unexpected error fetching GitHub skills")
        return FetchResult(error=f"Unexpected error: {e}")


def _is_skill_dir(md_path: str, tree: list[dict[str, Any]]) -> bool:
    """Check if an .md file is a standalone skill file (not a README, etc.)."""
    filename = md_path.rsplit("/", 1)[-1] if "/" in md_path else md_path
    # Exclude common non-skill files
    if filename.lower() in ("readme.md", "contributing.md", "changelog.md", "license.md"):
        return False
    return True


# ── skills.sh fetcher ────────────────────────────────────────────────────────


async def fetch_skills_sh() -> FetchResult:
    """Fetch skills from skills.sh registry.

    skills.sh provides a catalog of community skills. We attempt to use
    their JSON API if available, falling back to page scraping.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            # Try JSON API first
            api_url = "https://skills.sh/api/skills"
            resp = await client.get(api_url)

            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                if "json" in content_type:
                    return _parse_skills_sh_json(resp.json())

            # Fallback: try to fetch the index page and extract skill links
            resp = await client.get(SKILLS_SH_URL)
            if resp.status_code == 200:
                return _parse_skills_sh_html(resp.text)

            return FetchResult(error=f"skills.sh returned status {resp.status_code}")

    except httpx.HTTPError as e:
        return FetchResult(error=f"Network error connecting to skills.sh: {e}")
    except Exception as e:
        logger.exception("Unexpected error fetching skills.sh")
        return FetchResult(error=f"Unexpected error: {e}")


def _parse_skills_sh_json(data: Any) -> FetchResult:
    """Parse skills.sh JSON API response."""
    skills: list[RemoteSkillInfo] = []

    items = data if isinstance(data, list) else data.get("skills", data.get("items", []))
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name", item.get("title", ""))
        if not name:
            continue
        skills.append(
            RemoteSkillInfo(
                name=name,
                description=item.get("description", f"Skill: {name}"),
                source="skills_sh",
                url=item.get("url", item.get("download_url", f"https://skills.sh/skills/{name}")),
                repo_path=item.get("path", name),
            )
        )
    return FetchResult(skills=skills)


def _parse_skills_sh_html(html: str) -> FetchResult:
    """Parse skills from skills.sh HTML page (fallback)."""
    skills: list[RemoteSkillInfo] = []

    # Simple regex-free extraction of skill links
    # Look for patterns like <a href="/skills/..."> or data attributes
    import re

    # Pattern 1: Look for links to individual skill pages
    links = re.findall(r'href=["\'](/skills?/([^"\']+))["\']', html)
    seen: set[str] = set()
    for href, name in links:
        clean_name = name.strip("/").split("/")[0] if "/" in name else name.strip("/")
        if not clean_name or clean_name in seen:
            continue
        seen.add(clean_name)
        skills.append(
            RemoteSkillInfo(
                name=clean_name,
                description=f"Community skill: {clean_name}",
                source="skills_sh",
                url=f"https://skills.sh{href}",
                repo_path=clean_name,
            )
        )

    # Pattern 2: Look for JSON-LD or structured data
    json_blocks = re.findall(r'<script[^>]*type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    for block in json_blocks:
        try:
            import json
            data = json.loads(block)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item:
                        n = item["name"]
                        if n not in seen:
                            seen.add(n)
                            skills.append(
                                RemoteSkillInfo(
                                    name=n,
                                    description=item.get("description", f"Skill: {n}"),
                                    source="skills_sh",
                                    url=item.get("url", f"https://skills.sh/skills/{n}"),
                                    repo_path=n,
                                )
                            )
        except (ValueError, KeyError):
            continue

    if not skills:
        return FetchResult(error="Could not parse skills from skills.sh (page format may have changed)")
    return FetchResult(skills=skills)


# ── Install skill ────────────────────────────────────────────────────────────


async def install_remote_skill(
    skill: RemoteSkillInfo,
    dest_dir: Path,
) -> dict[str, Any]:
    """Download and install a single remote skill to the user skills directory.

    Returns dict with 'installed' name and 'path', or 'error'.
    """
    folder_name = skill.name.lower().replace(" ", "-").replace("/", "-")
    skill_dir = dest_dir / folder_name

    if skill_dir.exists():
        return {"error": f"Skill '{skill.name}' already installed at {skill_dir}"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            # For GitHub skills, download the raw SKILL.md
            if skill.source == "anthropics":
                resp = await client.get(skill.url)
                if resp.status_code != 200:
                    return {"error": f"Failed to download: HTTP {resp.status_code}"}
                content = resp.text
            elif skill.source == "skills_sh":
                # Try to get SKILL.md content from skills.sh
                # First try direct .md URL
                md_url = skill.url
                if not md_url.endswith(".md"):
                    md_url = skill.url.rstrip("/") + "/SKILL.md"
                resp = await client.get(md_url)
                if resp.status_code == 200 and _looks_like_markdown(resp.text):
                    content = resp.text
                else:
                    # Try the original URL and extract content
                    resp = await client.get(skill.url)
                    if resp.status_code != 200:
                        return {"error": f"Failed to download from skills.sh: HTTP {resp.status_code}"}
                    content = resp.text
                    if not _looks_like_markdown(content):
                        # Wrap HTML content as markdown
                        content = f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n\n# {skill.name}\n\n{skill.description}\n"
            else:
                return {"error": f"Unknown source: {skill.source}"}

        # Write to disk
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        return {"installed": skill.name, "path": str(skill_dir / "SKILL.md")}

    except httpx.HTTPError as e:
        return {"error": f"Network error: {e}"}
    except Exception as e:
        logger.exception("Failed to install skill %s", skill.name)
        return {"error": f"Install failed: {e}"}


async def _install_skill_async(
    owner: str,
    repo: str,
    skill_name: str,
    skill_path: str | None,
    task_id: str,
) -> dict[str, Any]:
    """Async version of install_skill_from_github."""
    dest_dir = get_skills_dir()
    skills = await fetch_github_skills(owner, repo)

    matched = [s for s in skills if s.name == skill_name]
    if not matched:
        return {"success": False, "error": f"Skill '{skill_name}' not found in {owner}/{repo}"}

    skill = matched[0]
    result = await install_remote_skill(skill, dest_dir)
    if "error" in result:
        return {"success": False, "error": result["error"]}
    return {"success": True, "name": result["installed"], "installed_path": result["path"]}


async def _install_all_skills_async(owner: str, repo: str, task_id: str) -> dict[str, Any]:
    """Async version of install_all_skills_from_repo."""
    dest_dir = get_skills_dir()
    skills = await fetch_github_skills(owner, repo)
    installed, failed = [], []

    for skill in skills:
        result = await install_remote_skill(skill, dest_dir)
        if "error" in result:
            failed.append(skill.name)
        else:
            installed.append(skill.name)

    return {
        "success": len(failed) == 0,
        "installed": installed,
        "failed": failed,
        "total": len(skills),
        "source_repo": f"{owner}/{repo}",
    }


def _looks_like_markdown(text: str) -> bool:
    """Heuristic check if text looks like Markdown content."""
    if text.strip().startswith(("<!DOCTYPE", "<html", "<HTML")):
        return False
    if text.strip().startswith(("---", "#", "##", "- ", "* ")):
        return True
    # Check for markdown indicators
    return any(marker in text for marker in ["# ", "## ", "```", "---\n"])


# ── Helper ───────────────────────────────────────────────────────────────────


def _parse_remote_skill(path: str, content: str) -> tuple[str, str]:
    """Extract name and description from a remote skill file."""
    import yaml

    # Derive default name from path
    parts = path.replace("\\", "/").split("/")
    if parts[-1] == "SKILL.md":
        default_name = parts[-2] if len(parts) >= 2 else "unknown"
    else:
        default_name = parts[-1].replace(".md", "")

    name = default_name
    description = ""

    # Try YAML frontmatter
    if content.startswith("---\n"):
        end_index = content.find("\n---\n", 4)
        if end_index != -1:
            try:
                metadata = yaml.safe_load(content[4:end_index])
                if isinstance(metadata, dict):
                    val = metadata.get("name")
                    if isinstance(val, str) and val.strip():
                        name = val.strip()
                    val = metadata.get("description")
                    if isinstance(val, str) and val.strip():
                        description = val.strip()
            except Exception:
                pass

    # Fallback: heading + first paragraph
    if not description:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                if name == default_name:
                    name = stripped[2:].strip() or default_name
                continue
            if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
                description = stripped[:200]
                break

    if not description:
        description = f"Remote skill: {name}"
    return name, description


# ── Local skills management ──────────────────────────────────────────────────

DEFAULT_SKILLS_DIR = Path.home() / ".openharness" / "skills"

SKILLS_SH_SOURCES = [
    ("anthropic/skills", "https://github.com/anthropics/skills"),
    ("vercel-labs/agent-skills", "https://github.com/vercel-labs/agent-skills"),
]


def get_skills_dir() -> Path:
    """Return the user skills install directory."""
    path = Path(os.environ.get("OPENHARNESS_SKILLS_DIR", str(DEFAULT_SKILLS_DIR)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_installed_skills() -> list[dict[str, Any]]:
    """List all skills installed in the user skills directory."""
    skills_dir = get_skills_dir()
    installed = []

    if not skills_dir.exists():
        return installed

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            name, description = _parse_remote_skill(content, skill_dir.name)
            installed.append({
                "name": name,
                "description": description,
                "path": str(skill_dir),
            })
        except Exception as exc:
            logger.warning("Failed to read skill %s: %s", skill_dir, exc)

    return installed


def remove_installed_skill(skill_name: str) -> dict[str, Any]:
    """Remove an installed skill from the user skills directory."""
    skills_dir = get_skills_dir()
    skill_dir = skills_dir / skill_name

    if not skill_dir.exists():
        return {"success": False, "error": f"Skill not found: {skill_name}"}

    try:
        shutil.rmtree(skill_dir)
        return {"success": True, "removed": skill_name}
    except OSError as exc:
        return {"success": False, "error": str(exc)}


def list_remote_skills() -> dict[str, Any]:
    """List available skills from known remote sources (sync wrapper)."""
    import asyncio

    try:
        result = asyncio.run(fetch_skills_sh())
        return {
            "skills": [
                {
                    "name": s.name,
                    "description": s.description,
                    "source": s.source,
                    "source_repo": s.source,
                    "url": s.url,
                }
                for s in result.skills
            ],
            "sources": [{"name": name, "url": url} for name, url in SKILLS_SH_SOURCES],
            "total": len(result.skills),
            "error": result.error,
        }
    except Exception as exc:
        logger.error("Failed to fetch remote skills: %s", exc)
        return {
            "skills": [],
            "sources": [{"name": name, "url": url} for name, url in SKILLS_SH_SOURCES],
            "total": 0,
            "error": str(exc),
        }


def install_skill_from_github(owner: str, repo: str, skill_name: str, skill_path: str | None = None) -> dict[str, Any]:
    """Install a specific skill from a GitHub repo (sync wrapper)."""
    import asyncio
    from uuid import uuid4

    try:
        task_id = uuid4().hex[:8]
        result = asyncio.run(_install_skill_async(owner, repo, skill_name, skill_path, task_id))
        return result
    except Exception as exc:
        logger.error("Failed to install skill: %s", exc)
        return {"success": False, "error": str(exc)}


def install_all_skills_from_repo(owner: str, repo: str) -> dict[str, Any]:
    """Install all skills from a GitHub repo (sync wrapper)."""
    import asyncio
    from uuid import uuid4

    try:
        task_id = uuid4().hex[:8]
        result = asyncio.run(_install_all_skills_async(owner, repo, task_id))
        return result
    except Exception as exc:
        logger.error("Failed to install all skills: %s", exc)
        return {"success": False, "error": str(exc), "installed": [], "failed": []}

