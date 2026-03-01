from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from ..framework.agent_types import AgentResult, Stage
from ..llm.service import LLMService
from ..llm.types import ChatMessage


@dataclass
class RequirementsAgentPlugin:
    """
    PLAN 阶段的需求分析 Agent：
    - 输入：ctx.task（自然语言任务描述）
    - 输出：结构化的需求说明书 JSON（写入 ctx.requirements 并持久化到 run_dir）
    """
    id: str = "requirements"
    stage: Stage = Stage.PLAN
    priority: int = 100

    def run(self, ctx, request=None) -> AgentResult:
        print("--- [DEBUG] RequirementsAgentPlugin.run() STARTED ---")
        llm: LLMService = ctx.services.get("llm") if ctx.services else None
        if not llm or not llm.enabled():
            note = "LLM 未配置，跳过需求分析阶段"
            ctx.events.emit("requirements.skip", {"reason": "no_llm"})
            ctx.requirements = None
            return AgentResult(status="skip", outputs={"notes": [note]})

        system = (
            "You are a senior software requirements analyst.\n"
            "You take a short natural language task description and produce a structured requirements spec.\n"
            "Your ONLY output must be a single valid JSON object, no markdown, no code fences.\n"
            "Use concise but precise Chinese where appropriate.\n"
        )
        user = (
            "根据下面的任务描述，生成一份结构化的软件需求说明书（JSON 对象）：\n\n"
            f"任务描述：{ctx.task}\n\n"
            "JSON schema 要求：\n"
            "{\n"
            '  "title": "简短的需求标题",\n'
            '  "summary": "用 1-3 句话总结整体目标",\n'
            '  "requirements": [\n'
            "    {\n"
            '      "id": "R1",\n'
            '      "description": "具体的功能或行为需求（可包含前置条件/后置条件）",\n'
            '      "rationale": "为什么需要这个需求（可选）",\n'
            '      "priority": "must|should|could",\n'
            '      "acceptance_criteria": [\n'
            '        "可验证的验收条件 1",\n'
            '        "可验证的验收条件 2"\n'
            "      ]\n"
            "    }\n"
            "  ],\n"
            '  "non_functional": [\n'
            '    "性能、可靠性、可维护性等非功能需求（如有）"\n'
            "  ],\n"
            '  "test_strategy": {\n'
            '    "overview": "如何验证上述需求（测试策略概述）",\n'
            '    "notes": ["额外的测试备注"]\n'
            "  }\n"
            "}\n\n"
            "必须遵守：\n"
            "1. 顶层输出只能是一个 JSON 对象，不能是数组。\n"
            "2. 字段名必须与 schema 完全一致，不要添加额外顶层字段。\n"
            "3. 所有字符串字段都必须是字符串类型，不能嵌套对象。\n"
            "4. 不要输出任何解释或 markdown，仅输出 JSON。\n"
        )

        messages: List[ChatMessage] = [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ]

        ctx.events.emit(
            "requirements.llm.call",
            {
                "provider": getattr(getattr(llm, "provider", None), "name", None),
                "model": getattr(llm, "model", None),
            },
        )

        resp = llm.generate_patch(messages)
        print(f"[DEBUG] LLM Response: {resp.get('content')}")
        if not resp.get("ok"):
            err = resp.get("error") or "LLM 调用失败"
            print(f"[DEBUG] Error: {err}")
            ctx.events.emit("requirements.error", {"error": err}, level="error")
            ctx.requirements = None
            return AgentResult(status="warn", outputs={"error": err})

        content = resp.get("content") or ""
        text = content.strip()
        if "```" in text:
            lines = []
            for line in text.splitlines():
                if line.strip().startswith("```"):
                    continue
                lines.append(line)
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("顶层必须是 JSON 对象")
        except Exception as e:
            err = f"无法解析需求 JSON：{e}"
            print(f"[DEBUG] Error: {err}")
            ctx.events.emit("requirements.parse_fail", {"error": err}, level="error")
            ctx.requirements = None
            return AgentResult(status="warn", outputs={"error": err})

        ctx.requirements = data

        # Save requirements.json into isolated target_workspace under repo_root
        repo_root = getattr(ctx, "repo_root", None)
        if repo_root is not None:
            workspace = Path(repo_root) / "target_workspace"
            try:
                workspace.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            try:
                req_path = workspace / "requirements.json"
                with req_path.open("w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                # Fallback to original behavior if direct file write fails
                ctx.save_json("requirements", data)
        else:
            # No repo_root on ctx; fall back to original behavior
            ctx.save_json("requirements", data)
        ctx.events.emit(
            "requirements.generated",
            {
                "title": data.get("title"),
                "req_count": len(data.get("requirements") or []),
            },
        )
        return AgentResult(status="ok", outputs={"requirements": data})
