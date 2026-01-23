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

        # 只重试一次（总共最多 2 次），避免无限循环
        max_retries = 1
        attempt = 0
        final_payload = None
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

            # Parse JSON payload from response
            payload, parse_error = self._parse_edits(response_text)
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
                            "你的输出无法解析为 JSON。请严格输出 JSON 对象（非数组），schema:\n"
                            "{\n"
                            "  \"action\": \"edit\" | \"multi_edit\",\n"
                            "  \"file_path\": \"path/to/file\",\n"
                            "  \"edits\": [ {\"old_string\": \"...\", \"new_string\": \"...\", \"expected_replacements\": 1 } ],\n"
                            "  \"message\": \"optional\"\n"
                            "}\n"
                            "禁止 ```json/``` 代码块，禁止解释文本。"
                        ),
                    ))
                    attempt += 1
                    last_error = parse_error
                    continue
                else:
                    ctx.events.emit("patch_author.skip", {"reason": "parse_fail_after_retry", "code_path": "parse_fail"})
                    return AgentResult(status="skip", outputs={"notes": [f"无法解析 LLM 输出: {parse_error}"]})

            # Normalize legacy shapes to protocol schema (best-effort)
            try:
                payload = self._normalize_protocol_payload(payload)
            except Exception as e:
                err_msg = f"协议校验失败: {e}"
                print("\n" + "="*80)
                print("❌ 编辑指令验证失败")
                print("="*80)
                print(f"错误: {err_msg}")
                print(f"\n📋 模型输出 (前 1500 字符):")
                print(json.dumps(payload, ensure_ascii=False)[:1500])
                print("="*80 + "\n")
                ctx.events.emit("patch.verify.fail", {"error": err_msg})
                last_error = err_msg
                if attempt < max_retries:
                    prompt_msgs.append(
                        ChatMessage(
                            role="user",
                            content=(
                                f"{err_msg}。\n"
                                "请输出 **单个 JSON 对象**（不是数组），并严格使用字段：action,file_path,edits,message。\n"
                                "edits 内每个元素只允许 old_string,new_string,expected_replacements。\n"
                                "不要在 edits 内再嵌套 file_path/search_block/replace_block。\n"
                                "如果需要改多个文件，请本轮只修改一个文件（使用顶层 file_path 指向该文件）。"
                            ),
                        )
                    )
                    attempt += 1
                    continue
                else:
                    ctx.events.emit("patch.apply.final_fail", {"error": err_msg})
                    return AgentResult(status="skip", outputs={"notes": [err_msg]})

            # Validate payload via protocol + executor dry-run
            from ..editing.protocol import parse_request
            from ..editing.executor import EditExecutor

            try:
                req = parse_request(payload)
            except Exception as e:
                err_msg = f"协议校验失败: {e}"
                print("\n" + "="*80)
                print("❌ 编辑指令验证失败")
                print("="*80)
                print(f"错误: {err_msg}")
                print(f"\n📋 模型输出 (前 1500 字符):")
                print(json.dumps(payload, ensure_ascii=False)[:1500])
                print("="*80 + "\n")
                ctx.events.emit("patch.verify.fail", {"error": err_msg})
                last_error = err_msg
                if attempt < max_retries:
                    prompt_msgs.append(ChatMessage(
                        role="user",
                        content=(
                            f"{err_msg}。请按编辑协议输出 JSON（禁止数组顶层，必须包含 action/file_path/edits/expected_replacements），"
                            "禁止 markdown 代码块，不要解释。"
                        ),
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
                final_payload = payload
                break
            else:
                err_msg = dry.error or "验证失败"
                print("\n" + "="*80)
                print("❌ 编辑指令验证失败")
                print("="*80)
                print(f"错误: {err_msg}")
                print(f"\n📋 生成的编辑指令 JSON (前 1500 字符):")
                print(json.dumps(payload, ensure_ascii=False)[:1500])
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

        if final_payload is None:
            ctx.events.emit("patch.apply.final_fail", {"error": last_error or "no_valid_payload"})
            return AgentResult(status="skip", outputs={"notes": [last_error or "no_valid_payload"]})

        # Save payload to file (raw JSON object text, NOT double-dumped)
        edit_text = json.dumps(final_payload, ensure_ascii=False, indent=2)
        edit_path = ctx.run_manager.save_patch(ctx, 1, edit_text)
        ctx.patch_queue.append(str(edit_path))
        
        count = len(final_payload.get("edits", [])) if isinstance(final_payload, dict) else 0
        ctx.events.emit("patch.proposed", {"count": count, "artifacts": [str(edit_path)]})
        return AgentResult(status="ok", artifacts=[str(edit_path)], outputs={"payload": final_payload})

    def _build_prompt(self, ctx, allowed_files: list[str]) -> list[ChatMessage]:
        # 1) 严格的 System Prompt，禁止 markdown 代码块，强调精确匹配与锚点
        system = r"""You are an Automated Code Refactoring Engine. You are NOT a chat assistant.
Your ONLY output must be a single RAW JSON OBJECT that conforms to the File Editing Protocol.

CRITICAL OUTPUT RULES
1) NO MARKDOWN, NO EXTRA TEXT: Output RAW JSON only. No leading/trailing characters.
2) VALID JSON: Must be parseable by a strict JSON parser. Use double quotes only.
3) STRING ENCODING:
   - Preserve newlines as \n within JSON strings.
   - Preserve tabs as \t if present.
   - Escape backslashes \\ and quotes \".
4) DO NOT GUESS: If required source text is missing from the provided context, output [].

EDIT CORRECTNESS RULES
5) EXACT MATCH: old_string MUST be byte-for-byte identical to the target file content
   (spaces, indentation, and newlines). Never reformat.
6) OCCURRENCES: old_string MUST occur exactly expected_replacements times in the file.
   If not, you MUST refine old_string or adjust expected_replacements accordingly.
7) STABLE ANCHORING: For insertions, choose old_string as a stable anchor (e.g., full function
   signature + surrounding braces) rather than only a short snippet.
8) MINIMAL CHANGE: Modify only what is necessary. Do not touch unrelated formatting.

ALLOWED FILES
- You may edit ONLY files listed in "allowed_files".
- If an operation targets a file not in allowed_files, omit it (do not include it).

PRE-FLIGHT SELF-CHECK (MANDATORY, SILENT)
Before producing output, internally verify:
- Every file_path is in allowed_files.
- Every old_string is present verbatim in the provided file content.
- old_string occurrences == expected_replacements.
If any check fails for an operation, drop that operation.

FILE EDITING PROTOCOL (STRICT)
{
  "action": "edit" | "multi_edit",
  "file_path": "path/to/file",
  "edits": [
    {
      "old_string": "exact original code content",
      "new_string": "new code content",
      "expected_replacements": 1
    }
  ],
  "message": "short note"
}"""

        # 2) 构造带边界的文件上下文，确保 search_block 来源明确
        file_contents_map = getattr(ctx, "file_contents", {}) or {}
        sections = []
        for f_path in allowed_files:
            content = file_contents_map.get(f_path)
            if content is None:
                content = self._read_file_content(ctx, f_path)
            sections.append(f"--- FILE: {f_path} ---\n{content}\n--- END OF {f_path} ---")
        file_context_str = "\n\n".join(sections)

        # 3) User Message，给出任务与文件内容（单文件协议：一次只改一个 file_path）
        need_tests = bool(getattr(ctx, "policy", {}).get("need_tests"))
        need_tests_line = (
            "IMPORTANT: You MUST add or update tests to cover the code changes in this iteration.\n"
            if need_tests
            else ""
        )

        diagnostics = self._build_diagnostics_block(ctx)
        user = (
            f"Task: {ctx.task}\n\n"
            "Output ONE JSON OBJECT that conforms to the schema. Do NOT output arrays at top-level.\n"
            "Do NOT include file_path inside edits. edits items must use old_string/new_string/expected_replacements.\n"
            "If you need to modify multiple files, in THIS response only modify ONE file.\n\n"
            + need_tests_line
            + diagnostics
            + f"{file_context_str}\n\n"
            "Output the JSON object now:"
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
            raw_text = None
            if getattr(ctx, "skills", None):
                res = ctx.skills.run("read_file", ctx, path=full_path, mode="raw")
                if res.ok:
                    raw_text = res.data
            if raw_text is None:
                raw_text = full_path.read_text(encoding="utf-8", errors="replace")
            lines = raw_text.splitlines(keepends=True)
            if len(lines) <= 300:
                content = "".join(lines)
            else:
                content = "".join(lines[:150]) + "\n... (omitted middle lines) ...\n" + "".join(lines[-150:])
            # cache for later validation/execution
            if hasattr(ctx, "file_contents"):
                ctx.file_contents[file_path] = content if len(lines) <= 300 else raw_text
            return content
        except Exception as e:
            return f"[Error reading file: {e}]"

    def _build_diagnostics_block(self, ctx) -> str:
        parts = []
        last_build = getattr(ctx, "last_build_result", None) or {}
        last_test = getattr(ctx, "last_test_result", None) or {}

        if last_build:
            summary = last_build.get("summary") or []
            if summary:
                lines = [f"{i+1}. {item.get('file','')}:{item.get('line','')} {item.get('message','')}" for i, item in enumerate(summary)]
                parts.append("BUILD ERRORS:\n" + "\n".join(lines))
        if last_test:
            summary = last_test.get("summary") or []
            if summary:
                lines = [f"{i+1}. {item.get('suite','')}.{item.get('case','')} {item.get('message','')}" for i, item in enumerate(summary)]
                parts.append("TEST FAILURES:\n" + "\n".join(lines))

        if not parts:
            return ""
        return "DIAGNOSTICS (from last run):\n" + "\n\n".join(parts) + "\n\n"

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

    def _normalize_protocol_payload(self, payload):
        """
        Accept ONLY the editing protocol object. Best-effort convert legacy shapes:
        - list of {file_path, search_block, replace_block} (same file) -> protocol object
        - object with edits[] using search_block/replace_block -> protocol object
        """
        # Legacy: top-level list of search/replace ops
        if isinstance(payload, list):
            if not payload:
                raise ValueError("edits 不能为空")
            if not all(isinstance(e, dict) for e in payload):
                raise ValueError("数组元素必须是对象")
            file_paths = {e.get("file_path") for e in payload}
            if None in file_paths or "" in file_paths:
                raise ValueError("数组元素缺少 file_path")
            if len(file_paths) != 1:
                raise ValueError("multi_edit 仅允许同一文件；请只输出一个 file_path 的 edits")
            fp = next(iter(file_paths))
            # Convert keys
            edits = []
            for e in payload:
                if "old_string" in e and "new_string" in e and "expected_replacements" in e:
                    edits.append({"old_string": e["old_string"], "new_string": e["new_string"], "expected_replacements": e["expected_replacements"]})
                elif "search_block" in e and "replace_block" in e:
                    edits.append({"old_string": e["search_block"], "new_string": e["replace_block"], "expected_replacements": 1})
                else:
                    raise ValueError("数组元素必须包含 old_string/new_string/expected_replacements 或 search_block/replace_block")
            return {"action": "multi_edit" if len(edits) > 1 else "edit", "file_path": fp, "edits": edits, "message": ""}

        if not isinstance(payload, dict):
            raise ValueError("Payload must be a JSON object")

        # Legacy object but edits items have search_block/replace_block
        if "action" not in payload and "file_path" not in payload and "edits" not in payload and "search_block" in payload:
            raise ValueError("Payload schema 不正确（顶层必须包含 action/file_path/edits）")

        if isinstance(payload.get("edits"), list):
            norm_edits = []
            for e in payload["edits"]:
                if not isinstance(e, dict):
                    raise ValueError("edits 元素必须是对象")
                if "old_string" in e and "new_string" in e and "expected_replacements" in e:
                    norm_edits.append({"old_string": e["old_string"], "new_string": e["new_string"], "expected_replacements": e["expected_replacements"]})
                elif "search_block" in e and "replace_block" in e:
                    norm_edits.append({"old_string": e["search_block"], "new_string": e["replace_block"], "expected_replacements": 1})
                else:
                    raise ValueError("edit 缺少 old_string/new_string/expected_replacements")
            payload = dict(payload)
            payload["edits"] = norm_edits
        return payload

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
