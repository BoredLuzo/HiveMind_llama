


import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional


@dataclass
class BuildStep:
    timestamp: str
    action: str           # "create", "edit", "delete", "test", "verify", "fail"
    file_path: Optional[str]
    success: bool
    summary: str          # Max 100 Zeichen


@dataclass
class ProjectState:
    chat_id: str
    workspace_path: str
    project_name: str
    created_at: str
    repomap_hash: Optional[str] = None
    plan_summary: Optional[str] = None
    plan_version: int = 0
    open_tasks: List[str] = field(default_factory=list)
    completed_tasks: List[str] = field(default_factory=list)
    build_history: List[BuildStep] = field(default_factory=list)
    last_run_timestamp: Optional[str] = None
    last_run_success: Optional[bool] = None
    total_runs: int = 0

    def add_build_step(self, action: str, file_path: Optional[str],
                       success: bool, summary: str, max_history: int = 20):
        step = BuildStep(
            timestamp=datetime.now().isoformat(),
            action=action,
            file_path=file_path,
            success=success,
            summary=summary[:100],
        )
        self.build_history.append(step)
        if len(self.build_history) > max_history:
            old = self.build_history[:5]
            summary_step = BuildStep(
                timestamp=old[0].timestamp,
                action="summary",
                file_path=None,
                success=all(s.success for s in old),
                summary=f"{len(old)} fruehe Steps zusammengefasst",
            )
            self.build_history = [summary_step] + self.build_history[5:]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectState":
        history = [BuildStep(**s) for s in data.get("build_history", [])]
        return cls(
            chat_id=data["chat_id"],
            workspace_path=data["workspace_path"],
            project_name=data["project_name"],
            created_at=data["created_at"],
            repomap_hash=data.get("repomap_hash"),
            plan_summary=data.get("plan_summary"),
            plan_version=data.get("plan_version", 0),
            open_tasks=data.get("open_tasks", []),
            completed_tasks=data.get("completed_tasks", []),
            build_history=history,
            last_run_timestamp=data.get("last_run_timestamp"),
            last_run_success=data.get("last_run_success"),
            total_runs=data.get("total_runs", 0),
        )


class ProjectStateManager:
    """Saves/loads ProjectState as JSON per chat_id."""

    _lgr = logging.getLogger("hivemind.project_state")
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, storage_dir: Path = Path("./context/projects")):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _get_lock(cls, chat_id: str) -> threading.RLock:
        if chat_id not in cls._locks:
            cls._locks[chat_id] = threading.RLock()
        return cls._locks[chat_id]

    def _path(self, chat_id: str) -> Path:
        safe = "".join(c for c in chat_id if c.isalnum() or c in "-_")
        return self.storage_dir / f"{safe}.project.json"

    def save(self, state: ProjectState):
        with self._get_lock(state.chat_id):
            path = self._path(state.chat_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(state.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)

    def load(self, chat_id: str) -> Optional[ProjectState]:
        with self._get_lock(chat_id):
            path = self._path(chat_id)
            if not path.exists():
                return None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return ProjectState.from_dict(data)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                ProjectStateManager._lgr.warning(
                    "[ProjectState] Korrupte JSON fuer chat_id=%s: %s — erstelle neu",
                    chat_id, e,
                )
                return None

    def create(self, chat_id: str, workspace_path: str,
               project_name: Optional[str] = None) -> ProjectState:
        with self._get_lock(chat_id):
            existing = self.load(chat_id)
            if existing is not None:
                return existing
            state = ProjectState(
                chat_id=chat_id,
                workspace_path=workspace_path,
                project_name=project_name or Path(workspace_path).name,
                created_at=datetime.now().isoformat(),
            )
            self.save(state)
            return state

    def delete(self, chat_id: str) -> bool:
        with self._get_lock(chat_id):
            path = self._path(chat_id)
            if path.exists():
                try:
                    path.unlink()
                    ProjectStateManager._lgr.info(
                        "[ProjectState] deleted for chat_id=%s", chat_id,
                    )
                    return True
                except OSError as e:
                    ProjectStateManager._lgr.warning(
                        "[ProjectState] delete failed chat_id=%s: %s", chat_id, e,
                    )
            return False


def build_project_context(ps: Optional[ProjectState]) -> str:
    if ps is None:
        return ""

    if ps.total_runs == 0:
        return (
            f"## New project: {ps.project_name}\n"
            f"Workspace: {ps.workspace_path}\n"
        )

    lines = [
        f"## Project: {ps.project_name}",
        f"**Workspace:** {ps.workspace_path}",
        f"**Runs so far:** {ps.total_runs}",
        f"**Plan version:** {ps.plan_version}" + (" (new)" if ps.plan_version <= 1 else ""),
        f"**Last run:** {ps.last_run_timestamp or 'Unknown'} "
        f"({'Success' if ps.last_run_success else 'Failed/Aborted' if ps.last_run_success is False else 'Unknown'})",
    ]
    if ps.plan_summary:
        lines.append(f"**Last plan:** {ps.plan_summary}")
    lines.extend(["", "### Build progress (last 5 steps)"])
    for step in ps.build_history[-5:]:
        icon = "OK" if step.success else "FAIL"
        file_info = f" ({step.file_path})" if step.file_path else ""
        lines.append(f"- [{icon}] {step.action}{file_info}: {step.summary}")

    if len(ps.build_history) > 5:
        lines.append(f"- ... and {len(ps.build_history) - 5} more steps")

    lines.append("")
    lines.append("### Open tasks")
    if ps.open_tasks:
        for task in ps.open_tasks:
            lines.append(f"- [ ] {task}")
    else:
        lines.append("No open tasks documented.")

    if ps.completed_tasks:
        lines.append("")
        lines.append(f"### Completed ({len(ps.completed_tasks)} tasks)")
        for task in ps.completed_tasks[-5:]:
            lines.append(f"- [x] {task}")

    return "\n".join(lines)
