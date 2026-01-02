## File Editing Protocol

用于约束 LLM 生成可执行的文件修改指令，避免不可应用的 diff/replace。

### 输出格式（JSON）
```json
{
  "action": "edit" | "multi_edit",
  "file_path": "path/to/file",
  "edits": [
    {
      "old_string": "EXACT original text, including whitespace and newlines",
      "new_string": "replacement text",
      "expected_replacements": 1
    }
  ],
  "message": "optional short rationale"
}
```

### 规则
- 仅允许 RAW JSON；禁止 markdown 代码块。
- `old_string` 必须与文件内容逐字节精确匹配，不得猜测或美化。
- `expected_replacements` 必填，执行器会校验出现次数是否一致，否则失败。
- `multi_edit` 仅限同一文件内多次替换，顺序执行，原子提交，任一步失败则不写回。
- 文件必须先被读取并缓存，否则拒绝执行。

### 常见失败原因
- `old_string` 不存在或出现次数与 `expected_replacements` 不一致。
- 输出中包含 ```json / ``` 代码块导致解析失败。
- 缺少必需字段或 action 非法。

### 产物
- 成功：返回 unified diff（基于旧/新内容生成）及已应用编辑信息。
- 失败：返回错误原因，不写回文件。

