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
                content_dbg = resp.get("content") or ""
                if content_dbg:
                    print("\n📥 模型返回内容 (前 2000 字符):")
                    print(content_dbg[:2000])
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
                        content=(
                            "你的输出无法解析为 JSON。请严格输出纯 JSON 数组，不要包含 ```json 或 ``` 之类的代码块标记，"
                            "不要输出任何解释或额外文本。必须包含至少一个编辑对象。"
                        ),
                    ))
                    attempt += 1
                    last_error = parse_error
                    continue
                else:
                    ctx.events.emit("patch_author.skip", {"reason": "parse_fail_after_retry", "code_path": "parse_fail"})
                    return AgentResult(status="skip", outputs={"notes": [f"无法解析 LLM 输出: {parse_error}"]})

            # Validate edits via protocol + executor dry-run
            from ..editing.protocol import parse_request
            from ..editing.executor import EditExecutor

            try:
                req = parse_request(edits)
            except Exception as e:
                err_msg = f"协议校验失败: {e}"
                print("\n" + "="*80)
                print("❌ 编辑指令验证失败")
                print("="*80)
                print(f"错误: {err_msg}")
                print(f"\n📋 模型输出 (前 1500 字符):")
                print(json.dumps(edits, ensure_ascii=False)[:1500])
                print("="*80 + "\n")
                ctx.events.emit("patch.verify.fail", {"error": err_msg})
                last_error = err_msg
                if attempt < max_retries:
                    prompt_msgs.append(ChatMessage(
                        role="user",
                        content=f"{err_msg}。请按编辑协议输出 JSON，并确保字段齐全。",
                    ))
                    attempt += 1
                    continue
                else:
                    ctx.events.emit("patch.apply.final_fail", {"error": err_msg})
                    return AgentResult(status="skip", outputs={"notes": [err_msg]})

            executor = EditExecutor(ctx.file_contents, Path(ctx.workspace))
            dry = executor.apply(req, dry_run=True)
            if dry.ok:
                ctx.events.emit("patch.verify.success", {"edit_count": len(req.edits)})
                final_edits = json.dumps(edits, ensure_ascii=False, indent=2)
                break
            else:
                err_msg = dry.error or "验证失败"
                print("\n" + "="*80)
                print("❌ 编辑指令验证失败")
                print("="*80)
                print(f"错误: {err_msg}")
                print(f"\n📋 生成的编辑指令 JSON (前 1500 字符):")
                print(json.dumps(edits, ensure_ascii=False)[:1500])
                print("="*80 + "\n")
                ctx.events.emit("patch.verify.fail", {"error": err_msg})
                last_error = err_msg
                if attempt < max_retries:
                    prompt_msgs.append(ChatMessage(
                        role="user",
                        content=f"验证失败：{err_msg}。仅修正 old_string 或 expected_replacements，再输出 JSON。",
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
        # 1) 严格的 System Prompt，禁止 markdown 代码块，强调精确匹配与锚点
        system = (
            "You are an Automated Code Refactoring Engine. You are NOT a chat assistant.\n"
            "Your task is to output a strict JSON array containing Search & Replace operations.\n\n"
            "### CRITICAL RULES\n"
            "1. **NO MARKDOWN**: Output RAW JSON only. Do NOT use ```json or ``` tags.\n"
            "2. **EXACT MATCH**: `search_block` must be a byte-for-byte copy from the source file "
            "(preserving all spaces, indents, and newlines). Do NOT reformat or beautify code.\n"
            "3. **UNIQUENESS**: Ensure `search_block` is unique in the file. Include more context lines if needed.\n"
            "4. **ANCHORING**: To add new code, `search_block` should anchor around stable context (e.g., "
            "the previous function's closing brace) so replacement can be applied deterministically.\n\n"
            "### JSON Schema\n"
            "[\n"
            "  {\n"
            '    "file_path": "path/to/file",\n'
            '    "search_block": "exact original code content",\n'
            '    "replace_block": "new code content"\n'
            "  }\n"
            "]\n"
        )

        # 2) 构造带边界的文件上下文，确保 search_block 来源明确
        file_contents_map = getattr(ctx, "file_contents", {}) or {}
        sections = []
        for f_path in allowed_files:
            content = file_contents_map.get(f_path)
            if content is None:
                content = self._read_file_content(ctx, f_path)
            sections.append(f"--- FILE: {f_path} ---\n{content}\n--- END OF {f_path} ---")
        file_context_str = "\n\n".join(sections)

        # 3) User Message，给出任务与文件内容
        user = (
            f"Task: {ctx.task}\n\n"
            "Based on the following file contents, generate the JSON array for Search & Replace.\n"
            "Remember: no markdown fences, raw JSON only, and search_block must be exact copies from the files.\n\n"
            f"{file_context_str}\n\n"
            "Output the JSON array now:"
        )

        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ]

    def _read_file_content(self, ctx, file_path: str) -> str:
        repo_root = Path(getattr(ctx.tool_router, "repo_root", "."))
        full_path = repo_root / file_path
        if not full_path.exists():
            return "[File not found]"
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                if len(lines) <= 300:
                    content = "".join(lines)
                else:
                    content = "".join(lines[:150]) + "\n... (omitted middle lines) ...\n" + "".join(lines[-150:])
                # cache for later validation/execution
                if hasattr(ctx, "file_contents"):
                    ctx.file_contents[file_path] = content if len(lines) <= 300 else full_path.read_text(encoding="utf-8", errors="replace")
                return content
        except Exception as e:
            return f"[Error reading file: {e}]"

    def _parse_edits(self, text: str) -> tuple[dict, str]:
        """Parse JSON payload from LLM response, handling markdown code blocks."""
        raw = text or ""
        text = raw.strip()

        # Remove markdown code fences anywhere in the text
        if "```" in text:
            stripped = []
            for line in text.split("\n"):
                if line.strip().startswith("```"):
                    continue
                stripped.append(line)
            text = "\n".join(stripped).strip()

        try:
            payload = json.loads(text)
            if not isinstance(payload, (dict, list)):
                return None, "输出必须是 JSON 对象或数组"
            return payload, None
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
