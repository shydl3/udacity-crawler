---
name: Tool Calling Rules
alwaysApply: true
---

# Tool Calling Rules

You are operating inside Continue IDE Agent.

- Use only tools explicitly provided in the current request.
- Never invent tool names or tool arguments.
- Do not call `edit_file` unless it is explicitly available.
- For editing existing files, use `edit_existing_file` if available.
- For creating files, use `create_new_file` if available.
- Before editing, read the existing file.
- Follow the exact argument schema for each tool.
- If the required tool is unavailable, explain the limitation.
- Never claim that a file has been modified unless a tool confirms success.
