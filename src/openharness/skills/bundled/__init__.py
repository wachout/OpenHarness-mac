"""Bundled skill definitions loaded from .md files."""

from __future__ import annotations

from pathlib import Path

from openharness.skills.types import SkillDefinition

_CONTENT_DIR = Path(__file__).parent / "content"


def get_bundled_skills() -> list[SkillDefinition]:
    """Load all bundled skills from the content/ directory.

    Supports both flat .md files and subdirectories with SKILL.md inside.
    """
    skills: list[SkillDefinition] = []
    if not _CONTENT_DIR.exists():
        return skills

    # Load flat .md files at root
    for path in sorted(_CONTENT_DIR.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        name, description = _parse_frontmatter(path.stem, content)
        skills.append(
            SkillDefinition(
                name=name,
                description=description,
                content=content,
                source="bundled",
                path=str(path),
            )
        )

    # Load skills from subdirectories (e.g., content/<skill-name>/SKILL.md)
    for child in sorted(_CONTENT_DIR.iterdir()):
        if child.is_dir():
            skill_path = child / "SKILL.md"
            if skill_path.exists():
                content = skill_path.read_text(encoding="utf-8")
                name, description = _parse_frontmatter(child.name, content)
                skills.append(
                    SkillDefinition(
                        name=name,
                        description=description,
                        content=content,
                        source="bundled",
                        path=str(skill_path),
                    )
                )
    return skills


def _parse_frontmatter(default_name: str, content: str) -> tuple[str, str]:
    """Extract name and description from a skill markdown file.

    Supports YAML frontmatter (``---`` delimited) and falls back to heading/paragraph parsing.
    """
    name = default_name
    description = ""
    lines = content.splitlines()

    # Try YAML frontmatter first
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                for fm_line in lines[1:i]:
                    fm = fm_line.strip()
                    if fm.startswith("name:"):
                        val = fm[5:].strip().strip("'\"")
                        if val:
                            name = val
                    elif fm.startswith("description:"):
                        val = fm[12:].strip().strip("'\"")
                        if val:
                            description = val
                break
        if description:
            return name, description

    # Fallback: heading + first paragraph
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            name = stripped[2:].strip() or default_name
            continue
        if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
            description = stripped[:200]
            break
    return name, description or f"Bundled skill: {name}"
