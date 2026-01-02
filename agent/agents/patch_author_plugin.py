from __future__ import annotations

import json
import os
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

        allowed_files = self._collect_allowed_files(ctx)
        ctx.events.emit("patch_author.allowed_files", {"count": len(allowed_files), "files": allowed_files})

        prompt_msgs = self._build_prompt(ctx, allowed_files)
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

        max_retries = 3
        attempt = 0
        final_edits = None
        last_error = None

        while attempt <= max_retries:
            if attempt > 0:
                ctx.events.emit("patch.retry", {"attempt": attempt, "error": last_error})

            ctx.events.emit(
                "llm.call",
                {"provider": getattr(llm.provider, "name", "unknown"), "model": llm.model, "attempt": attempt},
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
            response_text = resp.get("content", "") if isinstance(resp, dict) else ""
            
            ctx.events.emit(
                "llm.response",
                {
                    "ok": resp.get("ok"),
                    "latency_ms": resp.get("latency_ms"),
                    "response_bytes": len(response_text),
                    "error": resp.get("error"),
                },
                level="error" if not resp.get("ok") else "info",
            )

            if not resp.get("ok"):
                # Print debug info when LLM call fails
                print("\n" + "="*80)
                print("❌ LLM 调用失败")
                print("="*80)
                print(f"错误: {resp.get('error')}")
                if attempt == 0:
                    print(f"\n📝 Prompt (前 1000 字符):")
                    prompt_preview = "\n".join(m.content for m in prompt_msgs)[:1000]
                    print(prompt_preview)
                print("="*80 + "\n")
                
                ctx.events.emit("patch_author.skip", {"reason": resp.get("error") or "llm_failed", "code_path": "resp_not_ok"})
                return AgentResult(status="skip", outputs={"notes": [f"LLM 生成失败: {resp.get('error')}"]})

            # Parse JSON edits from response
            edits, parse_error = self._parse_edits(response_text)
            if parse_error:
                # Print debug info when parsing fails
                print("\n" + "="*80)
                print("❌ JSON 解析失败")
                print("="*80)
                print(f"错误: {parse_error}")
                print(f"\n📥 模型返回内容 (前 2000 字符):")
                print(response_text[:2000])
                print("="*80 + "\n")
                
                ctx.events.emit("patch.parse_fail", {"error": parse_error})
                if attempt < max_retries:
                    prompt_msgs.append(ChatMessage(
                        role="user",
                        content=f"你的输出无法解析为 JSON，错误：{parse_error}。请严格按照 JSON 格式输出，不要添加任何解释或 markdown 标记。必须包含至少一个编辑对象。",
                    ))
                    attempt += 1
                    last_error = parse_error
                    continue
                else:
                    ctx.events.emit("patch_author.skip", {"reason": "parse_fail_after_retry", "code_path": "parse_fail"})
                    return AgentResult(status="skip", outputs={"notes": [f"无法解析 LLM 输出: {parse_error}"]})

            # Validate edits
            if not edits:
                err_msg = "LLM 输出为空数组，请生成至少一个编辑指令"
                print("\n" + "="*80)
                print("❌ 编辑指令验证失败")
                print("="*80)
                print(f"错误: {err_msg}")
                print("="*80 + "\n")
                
                ctx.events.emit("patch.verify.fail", {"error": err_msg})
                last_error = err_msg
                if attempt < max_retries:
                    prompt_msgs.append(ChatMessage(
                        role="user",
                        content=f"{err_msg}。请生成至少一个包含 file_path/search_block/replace_block 的对象。",
                    ))
                    attempt += 1
                else:
                    ctx.events.emit("patch.apply.final_fail", {"error": err_msg})
                    return AgentResult(status="skip", outputs={"notes": [err_msg]})
                continue

            ctx.events.emit("patch.verify.start", {"edit_count": len(edits)})
            is_valid, err_msg = self._validate_edits(ctx, edits, allowed_files)
            
            if is_valid:
                ctx.events.emit("patch.verify.success", {"edit_count": len(edits)})
                final_edits = edits
                break
            else:
                # Print debug info when validation fails
                print("\n" + "="*80)
                print("❌ 编辑指令验证失败")
                print("="*80)
                print(f"错误: {err_msg}")
                print(f"\n📋 生成的编辑指令 (共 {len(edits)} 个):")
                import json
                print(json.dumps(edits, indent=2, ensure_ascii=False)[:1500])
                print("="*80 + "\n")
                
                ctx.events.emit("patch.verify.fail", {"error": err_msg})
                last_error = err_msg
                if attempt < max_retries:
                    prompt_msgs.append(ChatMessage(
                        role="user",
                        content=f"你的 Search & Replace 指令无法应用，错误：\n{err_msg}\n请检查 search_block 是否完全匹配文件内容（包括空格和换行），并重新生成。",
                    ))
                    attempt += 1
                else:
                    ctx.events.emit("patch.apply.final_fail", {"error": err_msg})
                    return AgentResult(status="skip", outputs={"notes": [f"编辑指令无法应用: {err_msg}"]})

        # Save edits to JSON file
        edit_path = ctx.run_manager.save_patch(ctx, 1, json.dumps(final_edits, indent=2, ensure_ascii=False))
        ctx.patch_queue.append(str(edit_path))
        
        ctx.events.emit("patch.proposed", {"count": len(final_edits), "artifacts": [str(edit_path)]})
        return AgentResult(status="ok", artifacts=[str(edit_path)], outputs={"edits": final_edits})

    def _build_prompt(self, ctx, allowed_files: list[str]) -> list[ChatMessage]:
        system = (
            "你是一个严谨的代码修复助手。你的任务是生成 Search & Replace 指令来修改代码。\n"
            "输出格式必须是纯 JSON 数组（不要使用 markdown 代码块），每个元素包含：\n"
            "- file_path: 文件路径（必须在 ALLOWED_FILES 中）\n"
            "- search_block: 要搜索的代码块（必须完全匹配文件内容，包括空格和换行）\n"
            "- replace_block: 替换后的代码块\n"
            "注意：search_block 必须在文件中唯一存在，否则无法应用。\n"
            "示例输出：\n"
            '[\n'
            '  {\n'
            '    "file_path": "demo_c_project/src/calculator.c",\n'
            '    "search_block": "int add(int a, int b) {\\n    return a + b;\\n}",\n'
            '    "replace_block": "int add(int a, int b) {\\n    return a + b;\\n}\\n\\nint mod(int a, int b) {\\n    return a % b;\\n}"\n'
            '  }\n'
            ']'
        )
        
        user_parts = [
            f"任务: {ctx.task}",
            "要求：",
            "1. 输出纯 JSON 数组（不要使用 ```json 等 markdown 标记）",
            "2. search_block 必须完全匹配文件内容",
            "3. 保持最小改动",
            "4. 只修改必要的文件",
            f"ALLOWED_FILES: {allowed_files}",
            "以下是目标文件的当前内容，请基于此内容生成 Search & Replace 指令："
        ]
        
        for f in allowed_files:
            content = self._read_file_content(ctx, f)
            user_parts.append(f"\n=== File: {f} ===\n{content}\n")

        if ctx.last_build_result and ctx.last_build_result.get('summary'):
            user_parts.append(f"\n构建错误摘要: {ctx.last_build_result.get('summary')}")
        if ctx.last_test_result and ctx.last_test_result.get('summary'):
            user_parts.append(f"\n测试失败摘要: {ctx.last_test_result.get('summary')}")
        
        user = "\n".join(user_parts)
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]

    def _read_file_content(self, ctx, file_path: str) -> str:
        repo_root = Path(getattr(ctx.tool_router, "repo_root", "."))
        full_path = repo_root / file_path
        if not full_path.exists():
            return "[File not found]"
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                if len(lines) <= 300:
                    return "".join(lines)
                return "".join(lines[:150]) + "\n... (omitted middle lines) ...\n" + "".join(lines[-150:])
        except Exception as e:
            return f"[Error reading file: {e}]"

    def _parse_edits(self, text: str) -> tuple[list[dict], str]:
        """Parse JSON edits from LLM response, handling markdown code blocks."""
        text = text.strip()
        
        # Remove markdown code blocks if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```json or ```)
            if lines[0].strip().startswith("```"):
                lines = lines[1:]
            # Remove last line if it's ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        
        try:
            edits = json.loads(text)
            if not isinstance(edits, list):
                return None, "输出必须是 JSON 数组"
            for i, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    return None, f"第 {i+1} 个元素不是 JSON 对象"
                if "file_path" not in edit:
                    return None, f"第 {i+1} 个元素缺少 file_path"
                if "search_block" not in edit:
                    return None, f"第 {i+1} 个元素缺少 search_block"
                if "replace_block" not in edit:
                    return None, f"第 {i+1} 个元素缺少 replace_block"
            return edits, None
        except json.JSONDecodeError as e:
            return None, f"JSON 解析失败: {e}"

    def _validate_edits(self, ctx, edits: list[dict], allowed_files: list[str]) -> tuple[bool, str]:
        """Validate that all edits can be applied."""
        repo_root = Path(getattr(ctx.tool_router, "repo_root", "."))
        allowed_set = set(allowed_files)
        
        for i, edit in enumerate(edits):
            file_path = edit["file_path"]
            search_block = edit["search_block"]
            
            # Check if file is allowed
            if file_path not in allowed_set:
                return False, f"文件 {file_path} 不在 ALLOWED_FILES 中"
            
            # Check if file exists
            full_path = repo_root / file_path
            if not full_path.exists():
                return False, f"文件 {file_path} 不存在"
            
            # Check if search_block exists in file
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                
                if search_block not in content:
                    return False, f"在文件 {file_path} 中找不到 search_block（第 {i+1} 个编辑）"
                
                # Check if search_block is unique
                if content.count(search_block) > 1:
                    return False, f"在文件 {file_path} 中 search_block 出现多次（第 {i+1} 个编辑），无法确定替换位置"
                    
            except Exception as e:
                return False, f"读取文件 {file_path} 失败: {e}"
        
        return True, ""

    def _collect_allowed_files(self, ctx) -> list[str]:
        files = []
        if ctx.context_pack:
            for item in ctx.context_pack.get("files", []):
                if isinstance(item, dict):
                    for m in item.get("matches", []):
                        path = m.split(":", 1)[0]
                        if path and path not in files:
                            files.append(path)
        if not files:
            files = [
                "demo_c_project/include/calculator.h",
                "demo_c_project/src/calculator.c",
                "demo_c_project/tests/test_calculator.cpp",
            ]
        return files[:10]

    def _extract_files_from_context(self, ctx) -> list:
        files = []
        if ctx.context_pack:
            for item in ctx.context_pack.get("files", []):
                if isinstance(item, dict):
                    matches = item.get("matches", [])
                    if matches:
                        files.append(matches[0].split(":")[0])
        return files[:10]
