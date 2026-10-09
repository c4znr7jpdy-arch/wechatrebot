"""Offline test catalog from shipped source; never import or run a plugin."""
from __future__ import annotations

import ast
import json

from build_legal_inventory import _simple_yaml


def _definitions(tree, owner, module):
    commands, tools = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            fields = {field.target.id: field.value.value for field in node.body
                      if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
                      and isinstance(field.value, ast.Constant)}
            if isinstance(fields.get("name"), str) and isinstance(fields.get("description"), str):
                tools.append({"plugin": owner, "name": fields["name"], "description": fields["description"]})
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            method = getattr(decorator.func, "attr", getattr(decorator.func, "id", ""))
            if method == "llm_tool":
                name = next((ast.literal_eval(k.value) for k in decorator.keywords if k.arg == "name"),
                            ast.literal_eval(decorator.args[0]) if decorator.args else node.name)
                tools.append({"plugin": owner, "name": name, "description": ast.get_docstring(node) or ""})
            if method not in {"command", "command_group"} or not decorator.args:
                continue
            base = decorator.func.value
            commands.append({"plugin": owner, "handler_name": node.name,
                             "handler_full_name": f"{owner}.{module}.{node.name}",
                             "fragment": ast.literal_eval(decorator.args[0]),
                             "parent": getattr(base, "id", getattr(base, "attr", "")),
                             "is_group": method == "command_group",
                             "aliases": sorted(next((ast.literal_eval(k.value) for k in decorator.keywords
                                                     if k.arg == "alias"), [])),
                             "description": ast.get_docstring(node) or ""})
    groups = {row["handler_name"]: row for row in commands if row["is_group"]}

    def command_name(row, seen=()):
        parent = groups.get(row["parent"])
        if parent and parent["handler_name"] not in seen:
            return command_name(parent, (*seen, parent["handler_name"])) + " " + row["fragment"]
        return row["fragment"]

    return [dict(row, effective_command=command_name(row)) for row in commands if not row["is_group"]], tools


def source_catalog(plugins_root, natural_source):
    snapshot = {"plugins": [], "commands": [], "declarations": [], "tools": [], "natural": []}
    for folder in sorted(plugins_root.iterdir()):
        if not (folder / "metadata.yaml").is_file():
            continue
        raw = (folder / "metadata.yaml").read_text(encoding="utf-8-sig")
        metadata = json.loads(raw) if raw.lstrip().startswith("{") else _simple_yaml(folder / "metadata.yaml")
        assert "name" in metadata, (folder.name, metadata)
        owner = metadata["name"].split(" #", 1)[0].strip()
        snapshot["plugins"].append({"name": owner, "display_name": metadata.get("display_name", owner),
                                    "description": metadata.get("desc", metadata.get("description", ""))})
        for source in sorted(folder.rglob("*.py")):
            if any(part in {"tests", "node_modules", ".venv", "__pycache__"} for part in source.parts):
                continue
            content = source.read_text(encoding="utf-8-sig")
            if not any(marker in content for marker in (".command(", ".command_group(", "llm_tool", "FunctionTool")):
                continue
            commands, tools = _definitions(ast.parse(content), owner, source.stem)
            snapshot["commands"].extend(commands)
            snapshot["tools"].extend(tools)
        declaration = folder / "capabilities.json"
        if declaration.is_file():
            for row in json.loads(declaration.read_text(encoding="utf-8-sig"))["capabilities"]:
                snapshot["declarations"].append(dict(row, id=f"decl:{owner}:{row['id']}", plugin=owner))
    # Repeated decorators can expose the same handler with more than one name.
    for kind, field in (("commands", "handler_full_name"), ("tools", "name")):
        snapshot[kind] = list({(row["plugin"], row[field]): row for row in snapshot[kind]}.values())
    for node in ast.walk(ast.parse(natural_source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CapabilitySpec":
            snapshot["natural"].append({k.arg: ast.literal_eval(k.value) for k in node.keywords
                                        if k.arg in {"capability_id", "name", "description", "aliases"}})
    return snapshot
