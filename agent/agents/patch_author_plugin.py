from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from ..patch_author import PatchAuthor
from ..framework.agent_types import AgentResult, Stage
from ..llm.types import ChatMessage
from ..llm.service import LLMService


@dataclass
class PatchAuthorPlugin:
    id: str = "patch_author"
    stage: Stage = Stage.EDIT
    priority: int = 100

    def __post_init__(self):
        self.agent = PatchAuthor(tool_router=None, run_manager=None)

    def run(self, ctx, request=None) -> AgentResult:
        self.agent.tool_router = ctx.tool_router
        self.agent.run_manager = ctx.run_manager
        llm: LLMService = ctx.services.get("llm") if ctx.services else None
        ctx.events.emit(
            "patch_author.enter",
            {
                "has_services": bool(ctx.services),
                "has_llm": bool(llm),
                "provider_name": getattr(getattr(llm, "provider", None), "name", None) if llm else None,
                "model": getattr(llm, "model", None) if llm else None,
            },
        )
        if not llm or not llm.enabled():
            ctx.events.emit("llm.skip", {"reason": "LLM provider not configured", "code_path": "no_llm"})
            ctx.events.emit("patch_author.skip", {"reason": "no_llm", "code_path": "no_llm"})
            return AgentResult(status="skip", outputs={"notes": ["LLM 未配置，跳过自动补丁"]})

        ctx.events.emit(
            "patch_author.config",
            {
                "provider": getattr(llm.provider, "name", None),
                "model": getattr(llm, "model", None),
                "timeout": getattr(llm, "timeout", None),
                "base_url_set": bool(getattr(llm.provider, "base_url", None)),
            },
        )

        prompt_msgs = self._build_prompt(ctx)
        if not prompt_msgs:
            ctx.events.emit("patch_author.skip", {"reason": "empty_prompt", "code_path": "no_prompt"})
            return AgentResult(status="skip", outputs={"notes": ["未生成 prompt，跳过自动补丁"]})
        ctx.events.emit(
            "patch_author.prompt",
            {
                "message_count": len(prompt_msgs),
                "approx_chars": sum(len(m.content) for m in prompt_msgs),
                "files_referenced": self._extract_files_from_context(ctx),
            },
        )

        ctx.events.emit(
            "llm.call",
            {"provider": getattr(llm.provider, "name", "unknown"), "model": llm.model},
        )
        ctx.events.emit(
            "llm.request",
            {
                "provider": getattr(llm.provider, "name", "unknown"),
                "model": llm.model,
                "timeout": llm.timeout,
                "approx_prompt_bytes": sum(len(m.content) for m in prompt_msgs),
            },
        )
        resp = llm.generate_patch(prompt_msgs)
        diff_text = resp.get("content", "") if isinstance(resp, dict) else ""
        diff_files_count = diff_text.count("diff --git")
        ctx.events.emit(
            "llm.response",
            {
                "ok": resp.get("ok"),
                "latency_ms": resp.get("latency_ms"),
                "diff_bytes": len(diff_text),
                "diff_files_count": diff_files_count,
                "error": resp.get("error"),
            },
            level="error" if not resp.get("ok") else "info",
        )
        if not resp.get("ok"):
            ctx.events.emit("patch_author.skip", {"reason": resp.get("error") or "llm_failed", "code_path": "resp_not_ok"})
            return AgentResult(status="skip", outputs={"notes": [f"LLM 生成失败: {resp.get('error')}"]})

        patches = [diff_text]
        artifacts = []
        for idx, patch in enumerate(patches, start=1):
            path = ctx.run_manager.save_patch(ctx, idx, patch)
            ctx.patch_queue.append(str(path))
            artifacts.append(str(path))
        ctx.events.emit("patch.proposed", {"count": len(patches), "artifacts": artifacts})
        return AgentResult(status="ok", artifacts=artifacts, outputs={"patches": patches})

    def _build_prompt(self, ctx) -> list[ChatMessage]:
        system = (
            "你是一个严谨的代码修复助手，只输出统一 diff 格式（unified diff），包含 diff --git。"
            "保持最小改动，不要添加无关代码。"
        )
        user_parts = [
            f"任务: {ctx.task}",
            "要求：只输出 unified diff，包含 diff --git；不得输出解释；保持现有风格；补丁行数尽量少。",
        ]
        if ctx.context_pack:
            files = ctx.context_pack.get("files", [])
            user_parts.append("相关文件示例（仅供参考，不要全量贴出）:")
            for item in files[:5]:
                if isinstance(item, dict):
                    matches = item.get("matches", [])
                    if matches:
                        user_parts.append(f"- {matches[0].split(':')[0]}")
        if ctx.last_build_result:
            user_parts.append(f"最近构建错误摘要: {ctx.last_build_result.get('summary')}")
        if ctx.last_test_result:
            user_parts.append(f"最近测试失败摘要: {ctx.last_test_result.get('summary')}")
        user = "\n".join(user_parts)
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]

    def _extract_files_from_context(self, ctx) -> list:
        files = []
        if ctx.context_pack:
            for item in ctx.context_pack.get("files", []):
                if isinstance(item, dict):
                    matches = item.get("matches", [])
                    if matches:
                        files.append(matches[0].split(":")[0])
        return files[:10]

