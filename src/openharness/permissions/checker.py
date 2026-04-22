"""Permission checking for tool execution."""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path

from openharness.config.settings import PermissionSettings
from openharness.permissions.modes import PermissionMode

log = logging.getLogger(__name__)

# Paths that are always denied regardless of permission mode or user config.
# These protect high-value credential and key material from LLM-directed access
# (including via prompt injection).  Patterns use fnmatch syntax and are matched
# against the fully-resolved absolute path produced by the query engine.
SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    # SSH keys and config
    "*/.ssh/*",
    # AWS credentials
    "*/.aws/credentials",
    "*/.aws/config",
    # GCP credentials
    "*/.config/gcloud/*",
    # Azure credentials
    "*/.azure/*",
    # GPG keys
    "*/.gnupg/*",
    # Docker credentials
    "*/.docker/config.json",
    # Kubernetes credentials
    "*/.kube/config",
    # OpenHarness own credential stores
    "*/.openharness/credentials.json",
    "*/.openharness/copilot_auth.json",
)


@dataclass(frozen=True)
class PermissionDecision:
    """Result of checking whether a tool invocation may run."""

    allowed: bool
    requires_confirmation: bool = False
    reason: str = ""


@dataclass(frozen=True)
class PathRule:
    """A glob-based path permission rule."""

    pattern: str
    allow: bool  # True = allow, False = deny


class PermissionChecker:
    """Evaluate tool usage against the configured permission mode and rules."""

    def __init__(
        self,
        settings: PermissionSettings,
        *,
        workspace_root: str | Path | None = None,
    ) -> None:
        self._settings = settings
        self._workspace_root = str(Path(workspace_root).resolve()) if workspace_root else None
        # Parse path rules from settings
        self._path_rules: list[PathRule] = []
        for rule in getattr(settings, "path_rules", []):
            pattern = getattr(rule, "pattern", None) or (rule.get("pattern") if isinstance(rule, dict) else None)
            allow = getattr(rule, "allow", True) if not isinstance(rule, dict) else rule.get("allow", True)
            if isinstance(pattern, str) and pattern.strip():
                self._path_rules.append(PathRule(pattern=pattern.strip(), allow=allow))
            else:
                log.warning(
                    "Skipping path rule with missing, empty, or non-string 'pattern' field: %r",
                    rule,
                )

    def _check_workspace_boundary(
        self,
        file_path: str | None,
        command: str | None,
    ) -> PermissionDecision | None:
        """Check if a write operation targets a path outside workspace_root.

        Returns a denial decision if the operation is out of bounds, or None
        if the operation is allowed or cannot be determined from the inputs.
        """
        assert self._workspace_root is not None

        # Check file-based tools (write_file, edit_file, etc.)
        if file_path:
            if not _is_path_under_root(file_path, self._workspace_root):
                return PermissionDecision(
                    allowed=False,
                    reason=(
                        f"Write operations are restricted to the workspace directory: "
                        f"{self._workspace_root}. "
                        f"Target path '{file_path}' is outside the workspace. "
                        f"Read-only access is allowed outside the workspace."
                    ),
                )
            return None

        # Check bash commands that write
        if command and _is_write_command(command):
            return PermissionDecision(
                allowed=False,
                reason=(
                    f"Write commands are restricted to the workspace directory: "
                    f"{self._workspace_root}. "
                    f"The command appears to modify files. "
                    f"If this command only operates within the workspace, "
                    f"try running it with an explicit path under the workspace."
                ),
                requires_confirmation=True,
            )

        return None

    def evaluate(
        self,
        tool_name: str,
        *,
        is_read_only: bool,
        file_path: str | None = None,
        command: str | None = None,
    ) -> PermissionDecision:
        """Return whether the tool may run immediately."""
        # Built-in sensitive path protection — always active, cannot be
        # overridden by user settings or permission mode.  This is a
        # defence-in-depth measure against LLM-directed or prompt-injection
        # driven access to credential files.
        if file_path:
            for candidate_path in _policy_match_paths(file_path):
                for pattern in SENSITIVE_PATH_PATTERNS:
                    if fnmatch.fnmatch(candidate_path, pattern):
                        return PermissionDecision(
                            allowed=False,
                            reason=(
                                f"Access denied: {file_path} is a sensitive credential path "
                                f"(matched built-in pattern '{pattern}')"
                            ),
                        )

        # Explicit tool deny list
        if tool_name in self._settings.denied_tools:
            return PermissionDecision(allowed=False, reason=f"{tool_name} is explicitly denied")

        # Explicit tool allow list
        if tool_name in self._settings.allowed_tools:
            return PermissionDecision(allowed=True, reason=f"{tool_name} is explicitly allowed")

        # Check path-level rules
        if file_path and self._path_rules:
            for candidate_path in _policy_match_paths(file_path):
                for rule in self._path_rules:
                    if fnmatch.fnmatch(candidate_path, rule.pattern):
                        if not rule.allow:
                            return PermissionDecision(
                                allowed=False,
                                reason=f"Path {file_path} matches deny rule: {rule.pattern}",
                            )

        # Check command deny patterns (e.g. deny "rm -rf /")
        if command:
            for pattern in getattr(self._settings, "denied_commands", []):
                if isinstance(pattern, str) and fnmatch.fnmatch(command, pattern):
                    return PermissionDecision(
                        allowed=False,
                        reason=f"Command matches deny pattern: {pattern}",
                    )

        # Full auto: allow everything
        if self._settings.mode == PermissionMode.FULL_AUTO:
            # Even in full_auto, enforce workspace_root write boundary
            if not is_read_only and self._workspace_root:
                boundary = self._check_workspace_boundary(file_path, command)
                if boundary is not None:
                    return boundary
            return PermissionDecision(allowed=True, reason="Auto mode allows all tools")

        # Read-only tools always allowed
        if is_read_only:
            return PermissionDecision(allowed=True, reason="read-only tools are allowed")

        # Workspace root boundary check for mutating tools
        if self._workspace_root:
            boundary = self._check_workspace_boundary(file_path, command)
            if boundary is not None:
                return boundary

        # Plan mode: block mutating tools
        if self._settings.mode == PermissionMode.PLAN:
            return PermissionDecision(
                allowed=False,
                reason="Plan mode blocks mutating tools until the user exits plan mode",
            )

        # Default mode: require confirmation for mutating tools
        bash_hint = _bash_permission_hint(command)
        reason = (
            "Mutating tools require user confirmation in default mode. "
            "Approve the prompt when asked, or run /permissions full_auto "
            "if you want to allow them for this session."
        )
        if bash_hint:
            reason = f"{reason} {bash_hint}"
        return PermissionDecision(
            allowed=False,
            requires_confirmation=True,
            reason=reason,
        )


def _policy_match_paths(file_path: str) -> tuple[str, ...]:
    """Return path forms that should participate in policy matching.

    Directory-scoped tools like ``grep`` and ``glob`` may operate on a root such
    as ``/home/user/.ssh``. Appending a trailing slash lets glob-style deny
    patterns like ``*/.ssh/*`` and ``/etc/*`` match the directory root itself.
    """
    normalized = file_path.rstrip("/")
    if not normalized:
        return (file_path,)
    return (normalized, normalized + "/")


def _is_path_under_root(file_path: str, root: str) -> bool:
    """Check whether a resolved file_path falls under the workspace root."""
    try:
        resolved = Path(file_path).resolve()
        root_resolved = Path(root).resolve()
        return str(resolved).startswith(str(root_resolved) + "/") or resolved == root_resolved
    except (OSError, ValueError):
        return False


_WRITE_COMMAND_PREFIXES: tuple[str, ...] = (
    "rm ",
    "rm -",
    "mv ",
    "cp ",
    "mkdir ",
    "mkdir -",
    "rmdir ",
    "chmod ",
    "chown ",
    "ln -",
    "ln ",
    "touch ",
    "truncate ",
    "tee ",
    "dd ",
    "install ",
)

_WRITE_COMMAND_TOKENS: tuple[str, ...] = (
    " > ",
    " >> ",
    "|tee ",
    ">>",
)


def _is_write_command(command: str) -> bool:
    """Heuristic: determine whether a bash command performs file mutations."""
    stripped = command.strip()
    for prefix in _WRITE_COMMAND_PREFIXES:
        if stripped.startswith(prefix):
            return True
    for token in _WRITE_COMMAND_TOKENS:
        if token in stripped:
            return True
    # Handle redirection at start: `> file` or `>> file`
    if stripped.startswith(">"):
        return True
    return False


def _bash_permission_hint(command: str | None) -> str:
    if not command:
        return ""
    lowered = command.lower()
    install_markers = (
        "npm install",
        "pnpm install",
        "yarn install",
        "bun install",
        "pip install",
        "uv pip install",
        "poetry install",
        "cargo install",
        "create-next-app",
        "npm create ",
        "pnpm create ",
        "yarn create ",
        "bun create ",
        "npx create-",
        "npm init ",
        "pnpm init ",
        "yarn init ",
    )
    if any(marker in lowered for marker in install_markers):
        return (
            "Package installation and scaffolding commands change the workspace, "
            "so they will not run automatically in default mode."
        )
    return ""
