from __future__ import annotations
import asyncio
import json
import re
import time
import uuid
import logging
from dataclasses import dataclass

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    _rich_available = True
except ImportError:
    _rich_available = False
    Console = Panel = Table = None  # type: ignore

import sys
import os

_this_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.dirname(_this_dir)

if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)
# Lazy import: make_client is only needed when Pipeline is instantiated.
# Top-level import triggers backend connection setup at module load time,
# which is unnecessary when server.py only uses Pipeline as a config container.
from hive_functions.prompts import PROMPTS, AGENT_ROLES
from hive_functions.memory import Memory
from infra.config import (
    DEFAULT_MODEL, MAX_ITERATIONS, SESSIONS_DIR,
    MODEL_ANALYST, MODEL_REFINER, MODEL_CRITIC,
    MODEL_SYNTHESIZER, MODEL_DIRECT, MODEL_JUDGE,
)

_log = logging.getLogger("hivemind.pipeline")

console = Console() if _rich_available else None

SELF_TRIGGERS = [
    "what are you", "how do you work", "describe yourself",
    "what can you do", "what is hivemind", "what are you built from",
    "who are you", "introduce yourself",
    "was bist du", "wer bist du", "wie funktionierst du", "erkläre dich",
    "was kannst du", "wie bist du aufgebaut", "was ist hivemind",
    "wie arbeitest du", "stell dich vor", "welche modelle", "welches modell",
]


def _has_keyword(text: str, keywords) -> bool:


    _t = (text or "").lower()
    for _kw in keywords or []:
        _k = str(_kw).strip().lower()
        if not _k:
            continue
        if re.search(rf"\b{re.escape(_k)}\b", _t):
            return True
    return False

AGENT_PREFIX_PATTERN = re.compile(
    r'^@?(analyst|refiner|critic|kritiker)\s*[:\-]?\s*(.+)',
    re.IGNORECASE
)
AGENT_NAME_MAP = {
    "analyst": "analyst", "refiner": "refiner",
    "critic": "critic", "kritiker": "critic",
}


try:
    from hive_functions.num_ctx_config import get_num_ctx as _get_num_ctx
except ImportError:
    _log.warning(
        "num_ctx_config.py not found - num_ctx limits disabled in pipeline.py! "
        "KV-Cache overflow possible."
    )
    def _get_num_ctx(model: str, agent_role: str | None = None) -> int | None:  # type: ignore
        return None


@dataclass
class AgentConfig:
    key:         str
    name:        str
    model:       str
    temperature: float
    max_tokens:  int
    thinking:        bool = False
    thinking_budget: int = 0

    def display(self) -> str:
        return f"[cyan]{self.name}[/] → [yellow]{self.model}[/]"


@dataclass
class StepResult:
    agent:   str
    model:   str
    content: str
    elapsed: float


class Pipeline:
    # Bei uvicorn reload=True: alte Pipeline-Instanz + neue teilen _bg_tasks → GC-Leak.

    def __init__(self, memory: Memory = None, agent_overrides: dict = None):


        self._bg_tasks: set = set()
        self._last_judge_verdict: dict = {}
        self.memory = memory or Memory()
        from backend import make_client as _make_client
        self.ollama = _make_client()
        self.steps: list[StepResult] = []

        overrides = agent_overrides or {}

        self.agents: dict[str, AgentConfig] = {
            "analyst": AgentConfig(
                key="analyst", name="Analyst",
                model=overrides.get("analyst", MODEL_ANALYST),
                temperature=0.2, max_tokens=900,
            ),
            "refiner": AgentConfig(
                key="refiner", name="Refiner",
                model=overrides.get("refiner", MODEL_REFINER),
                temperature=0.3, max_tokens=900,  # Refiner verarbeitet vollen Analyst-Output
            ),
            "critic": AgentConfig(
                key="critic", name="Kritiker",
                model=overrides.get("critic", MODEL_CRITIC),
                temperature=0.2, max_tokens=350,
            ),
            "synthesizer": AgentConfig(
                key="synthesizer", name="Synthesizer",
                model=overrides.get("synthesizer", MODEL_SYNTHESIZER),
                temperature=0.2, max_tokens=800,
            ),
            "direct": AgentConfig(
                key="direct", name="Answer",
                model=overrides.get("direct", MODEL_DIRECT),
                temperature=0.4, max_tokens=600,
            ),
            "judge": AgentConfig(
                key="judge", name="Judge",
                model=overrides.get("judge", MODEL_JUDGE),
                temperature=0.1, max_tokens=120,
            ),
            # ── Duo Mode Agent-Slots ───────────────────────────────────────────
            "vision": AgentConfig(
                key="vision", name="Vision-Agent",
                model=overrides.get("vision", ""),
                temperature=0.2, max_tokens=600,    # Bildbeschreibung/Analyse: ~400-500 Tokens reicht
            ),
            "duo_coder": AgentConfig(
                key="duo_coder", name="Duo-Coder",
                model=overrides.get("duo_coder", ""),
                temperature=0.8, max_tokens=12000,
            ),
            "duo_critic": AgentConfig(
                key="duo_critic", name="Duo-Critic",
                model=overrides.get("duo_critic", ""),
                temperature=0.1, max_tokens=400,
            ),
        }

    def get_active_models(self) -> list[str]:
        return list(set(a.model for a in self.agents.values()))

    def set_agent_model(self, agent_key: str, model: str):
        if agent_key in self.agents:
            self.agents[agent_key].model = model
            return True
        return False

    def set_all_models(self, model: str):
        for agent in self.agents.values():
            agent.model = model

    def print_config(self):
        """Shows the current agent/model configuration."""
        table = Table(title="Hivemind Configuration", border_style="yellow")
        table.add_column("Agent", style="cyan")
        table.add_column("Model", style="yellow")
        table.add_column("Temp", style="dim")
        table.add_column("Max Tokens", style="dim")
        for agent in self.agents.values():
            table.add_row(
                agent.name,
                agent.model,
                str(agent.temperature),
                str(agent.max_tokens),
            )
        if console:
            console.print(table)

    # ── Message-Builder ────────────────────────────────────────

    def _build_messages(self, system: str, user: str,
                        use_session: bool = True,
                        use_memory: bool = True) -> list[dict]:
        mem_ctx  = self.memory.as_context_string() if use_memory else ""
        sess_ctx = self.memory.get_session_context() if use_session else ""
        full_system = system + (f"\n\n{mem_ctx}" if mem_ctx else "")
        full_user   = (f"{sess_ctx}\n\nCurrent question: {user}" if sess_ctx else user)
        return [{"role": "system", "content": full_system},
                {"role": "user",   "content": full_user}]

    # ── Agent-Calls ────────────────────────────────────────────

    async def _call_raw(self, model: str, system: str, user: str,
                        temperature: float = 0.1, max_tokens: int = 100,
                        agent_role: str | None = None) -> str:
        messages = [{"role": "system", "content": system},
                    {"role": "user",   "content": user}]
        num_ctx = _get_num_ctx(model, agent_role)
        kwargs = {"ctx": num_ctx} if num_ctx is not None else {}
        return await self.ollama.chat(model, messages, temperature, max_tokens, **kwargs)

    async def _call_stream(self, agent_key: str, user: str,
                           label: str = "",
                           use_session: bool = True,
                           use_memory: bool = True,
                           custom_system: str = None,
                           custom_model: str = None,
                           custom_max_tokens: int = None) -> str:
        agent    = self.agents[agent_key]
        model    = custom_model or agent.model
        max_tok  = custom_max_tokens or agent.max_tokens
        temp     = agent.temperature
        system   = custom_system or PROMPTS.get(agent_key, "")  # KeyError-safe
        messages = self._build_messages(system, user, use_session, use_memory)
        num_ctx  = _get_num_ctx(model, agent_role=agent_key)

        t = time.time()
        parts = []
        display_label = label or agent.name
        if console:
            console.print(f"\n[bold cyan]▶ {display_label}[/] [dim]({model})[/]")

        kwargs = {"ctx": num_ctx} if num_ctx is not None else {}
        try:
            async for token in self.ollama.chat_stream(
                    model, messages, temp, max_tok, **kwargs):
                parts.append(token)
                if console:
                    console.print(token, end="", highlight=False)
        except Exception as e:
            err = f"[{display_label}-error: {str(e)[:120]}]"
            if console:
                console.print(f"\n[bold red]{err}[/]")
            parts.append(err)

        if console:
            console.print()
        content = "".join(parts)
        elapsed = time.time() - t
        if console:
            console.print(f"[dim]  ({elapsed:.1f}s)[/]")
        self.steps.append(StepResult(display_label, model, content, elapsed))
        return content

    # ── Klassifizierung ────────────────────────────────────────

    async def _check_complexity(self, user_input: str) -> str:
        self._last_judge_verdict = {}
        try:
            #
            try:
                from backend.llama_server_manager import manager as _mgr
                judge_model = self.agents["judge"].model
                if _mgr.get_port_for(judge_model) is None:
                    self._last_judge_verdict = {
                        "level": "simple",
                        "route": "direct",
                        "task_type": "general",
                        "tool_model": "small",
                        "reason": "judge_cold",
                    }
                    return "simple"
            except Exception:
                pass

            result = await self._call_raw(
                self.agents["judge"].model,
                PROMPTS["complexity_judge"],
                user_input, 0.1, self.agents["judge"].max_tokens,
            )
            # TUNE-PARSER: "level=complex route=duo type=code tool=small reason=..."
            # Fallback: altes JSON-Format
            def _parse_judge(text: str) -> dict:
                d = {}
                for m in re.finditer(r'(\w+)=([^\s,.:;<>]+)', text):
                    d[m.group(1)] = m.group(2).strip("<>")
                reason_match = re.search(r'reason=(.+)', text, re.IGNORECASE)
                if reason_match:
                    d["reason"] = reason_match.group(1).strip().rstrip('.,;>')
                return d
            data = _parse_judge(result)
            if "level" not in data:
                jm = re.search(r'\{[^{}]+\}', result, re.DOTALL)
                if jm:
                    try:
                        jd = json.loads(jm.group())
                        data = {"level": jd.get("level",""), "route": jd.get("route","direct"),
                                "type": jd.get("task_type","general"), "tool": jd.get("tool_model","small"),
                                "reason": jd.get("reason","")}
                    except (json.JSONDecodeError, Exception):
                        pass
            level = data.get("level", "simple")
            if level in ("trivial", "simple", "complex"):
                self._last_judge_verdict = {
                    "level":      level,
                    "route":      data.get("route", "direct"),
                    "task_type":  data.get("type", "general"),
                    "tool_model": data.get("tool", "small"),
                    "reason":     data.get("reason", ""),
                }
                return level
        except Exception as e:
            # "simple" classified → Coder route never activated.
            _log.warning("Judge error, falling back to 'simple': %s", e)

        if not self._last_judge_verdict:
            self._last_judge_verdict = {
                "level": "simple", "route": "direct",
                "task_type": "general", "tool_model": "small", "reason": "fallback",
            }
        return "simple"

    def _parse_agent_call(self, text: str) -> tuple[str, str] | None:
        match = AGENT_PREFIX_PATTERN.match(text.strip())
        if match:
            raw_name  = match.group(1).lower()
            question  = match.group(2).strip()
            agent_key = AGENT_NAME_MAP.get(raw_name)
            if agent_key:
                return agent_key, question
        return None

    def _is_self_question(self, t: str) -> bool:
        return _has_keyword(t, SELF_TRIGGERS)

    def _is_memory_request(self, t: str) -> bool:
        if self._is_forget_request(t):
            return False
        return _has_keyword(t,
            ["remember", "remember that", "store", "save this", "keep in mind", "note",
             "merke dir", "merk dir", "speichere", "denke daran", "behalte", "notiere"])

    def _is_forget_request(self, t: str) -> bool:
        return _has_keyword(t, ["forget", "delete", "remove", "vergiss", "lösche", "lösch"])

    def _is_list_memory_request(self, t: str) -> bool:
        return _has_keyword(t,
            ["show memory", "list memory", "what do you know", "what have you stored",
             "was weißt du", "was hast du gespeichert", "zeig memory",
             "liste memory"])

    async def _handle_memory_request(self, text: str) -> str:
        result = await self._call_raw(
            self.agents["judge"].model,
            PROMPTS["memory_extractor"], text, 0.1, 100,
        )
        try:
            match = re.search(r'\{[^{}]+\}', result, re.DOTALL)
            if match:
                data  = json.loads(match.group())
                key   = data.get("key", "").strip()
                value = data.get("value", "").strip()
                if key and value:
                    self.memory.remember(key, value)
                    return f"✅ Saved: {value}"
        except Exception:
            pass
        return "⚠️ Could not extract a fact."

    # ── Direct agent call ──────────────────────────────────────

    async def _run_single_agent(self, agent_key: str, question: str) -> str:
        agent = self.agents[agent_key]
        role  = AGENT_ROLES.get(agent_key, "")
        custom_system = (
            PROMPTS.get("agent_direct", "")
            .replace("{agent_name}", agent.name)
            .replace("{agent_role}", role)
        )
        if console:
            console.print(Panel(
                f"[bold]{agent.name}[/] [dim]({agent.model})[/]\n{question}",
                border_style="cyan"
            ))
        return await self._call_stream(
            agent_key, question,
            label=agent.name,
            use_session=True, use_memory=True,
            custom_system=custom_system,
        )


    async def run(self, user_input: str) -> str:


        self.steps = []
        t_total = time.time()

        if self._is_list_memory_request(user_input):
            memories = self.memory.list_memories()
            if not memories:
                if console:
                    console.print("[dim]Nothing saved.[/]")
            else:
                for k, v, d in memories:
                    if console:
                        console.print(f"  [cyan]{k}[/]: {v} [dim]({d})[/]")
            return ""

        if self._is_forget_request(user_input):
            q_lower = user_input.lower()
            deleted = [k for k in list(self.memory.get_all()) if k.lower() in q_lower]
            for k in deleted:
                self.memory.forget(k)
            msg = (f"✅ Deleted: {', '.join(deleted)}" if deleted else
                   f"⚠️ Nothing found. Keys: {', '.join(self.memory.get_all())}")
            console.print(msg) if console else None
            return msg

        if self._is_memory_request(user_input):
            result = await self._handle_memory_request(user_input)
            self.memory.add_to_session("user", user_input)
            self.memory.add_to_session("assistant", result)
            if console:
                console.print(result)
            return result

        if self._is_self_question(user_input):
            model_info = "\n".join(
                f"- {a.name}: {a.model}"
                for a in self.agents.values()
                if a.key not in ("judge", "direct")
            )
            custom_self = PROMPTS["self"] + f"\n\nCurrent model configuration:\n{model_info}"
            answer = await self._call_stream(
                "direct", user_input,
                label="Hivemind — Self-Info",
                use_session=False, use_memory=False,
                custom_system=custom_self,
                custom_model=self.agents["direct"].model,
            )
            self.memory.add_to_session("user", user_input)
            self.memory.add_to_session("assistant", answer)
            return answer

        agent_call = self._parse_agent_call(user_input)
        if agent_call:
            agent_key, question = agent_call
            answer = await self._run_single_agent(agent_key, question)
            self.memory.add_to_session("user", user_input)
            self.memory.add_to_session("assistant", answer)
            if console:
                console.print(f"[dim]Total: {time.time() - t_total:.1f}s[/]")
            return answer

        if console:
            console.print("[dim]Assessing complexity...[/]", end="\r")
        complexity = await self._check_complexity(user_input)
        if console:
            console.print(f"[dim]Complexity: {complexity}          [/]")

        if complexity in ("trivial", "simple"):
            answer = await self._call_stream(
                "direct", user_input, use_session=True, use_memory=True)
            self.memory.add_to_session("user", user_input)
            self.memory.add_to_session("assistant", answer)
            if console:
                console.print(f"[dim]Total: {time.time() - t_total:.1f}s[/]")
            return answer

        if console:
            console.print(Panel(
                f"[bold yellow]{user_input}[/]",
                title="🧠 Analysis Pipeline", border_style="yellow"
            ))
        previous      = ""
        full_previous = ""

        _mem_ctx = self.memory.as_context_string()
        def _agent_system(key: str) -> str:
            base = PROMPTS.get(key, "")
            return base + (f"\n\n{_mem_ctx}" if _mem_ctx else "")

        for i in range(MAX_ITERATIONS):
            if console:
                console.rule(f"[bold]Round {i + 1} / {MAX_ITERATIONS}[/]")

            analyst_input = user_input
            if previous:
                analyst_input += f"\n\nPrevious analysis:\n{previous}\n\nImprove it."

            analyst_out = await self._call_stream(
                "analyst", analyst_input,
                use_session=False, use_memory=False,
                custom_system=_agent_system("analyst"))
            refiner_out = await self._call_stream(
                "refiner",
                f"Original question: {user_input}\n\nAnalysis:\n{analyst_out}\n\nImprove ONLY the analysis above.",
                use_session=False, use_memory=False,
                custom_system=_agent_system("refiner"),
            )
            critic_out = await self._call_stream(
                "critic",
                f"Problem: {user_input}\n\nAnalysis:\n{refiner_out}\n\nWhat is being overlooked? What gaps does this analysis have?",
                use_session=False, use_memory=False,
                custom_system=_agent_system("critic"),
            )
            _sp = []
            if analyst_out: _sp.append(f"[Analysis]\n{analyst_out}")
            if refiner_out: _sp.append(f"[Refinement]\n{refiner_out}")
            if critic_out:  _sp.append(f"[Critique]\n{critic_out}")
            full_previous = "\n\n".join(_sp)
            # Carry over enough refined context for the next analyst pass.
            # Limit scales with analyst context size but remains bounded.
            _analyst_ctx = _get_num_ctx(self.agents["analyst"].model, "analyst") or 4096
            _ctx_limit = max(2400, min(12000, int(_analyst_ctx * 2)))
            previous = refiner_out if len(refiner_out) <= _ctx_limit else refiner_out[:_ctx_limit]

        if console:
            console.rule("[bold green]Synthesis[/]")

        if not full_previous:
            final = await self._call_stream(
                "direct", user_input, use_session=True, use_memory=True)
        else:
            final = await self._call_stream(
                "synthesizer",
                (
                    f"[INTERNAL INTERMEDIATE ANALYSES]\n{full_previous}\n[END]\n\n"
                    f"User question: {user_input}\n\n"
                    f"Write the final answer directly for the user."
                ),
                use_session=True, use_memory=True,
            )

        self.memory.add_to_session("user", user_input)
        self.memory.add_to_session("assistant", final)
        if console:
            console.print(Panel(final, title="🏆 Result", border_style="green"))
            console.print(f"[dim]Total: {time.time() - t_total:.1f}s[/]")
        _t = asyncio.create_task(self._save_session_async(user_input, final))
        self._bg_tasks.add(_t)
        _t.add_done_callback(self._bg_tasks.discard)
        return final

    def _save_session(self, problem: str, result: str):
        _ts = time.strftime("%Y%m%dT%H%M%S")
        path = SESSIONS_DIR / f"session_{_ts}_{time.time_ns()}_{uuid.uuid4().hex[:6]}.json"
        data = {
            "problem": problem, "result": result,
            "steps": [{"agent": s.agent, "model": s.model,
                       "content": s.content, "elapsed": s.elapsed}
                      for s in self.steps],
        }
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            if console:
                console.print(f"[dim yellow]? Session save failed: {e}[/]")

    async def _save_session_async(self, problem: str, result: str):
        await asyncio.to_thread(self._save_session, problem, result)

    async def close(self):
        await self.ollama.close()