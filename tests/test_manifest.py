"""规范自检的本地版：主程序那边有完整 checker，这里保证插件自己不会退化。

规则来自 docs/plugin-spec.md：清单必备字段、入口有 setup、panel 有页面、只 import app.sdk。
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SKIP_DIRS = {"__pycache__", ".git", ".venv"}


def manifest() -> dict:
    return tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))


def python_files() -> list[Path]:
    return [
        path for path in ROOT.rglob("*.py")
        if not (set(path.relative_to(ROOT).parts) & SKIP_DIRS)
    ]


def imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names if base)
    return names


def test_manifest_fields():
    data = manifest()
    assert data.get("title"), "plugin.toml 缺 title"
    assert SEMVER.match(str(data.get("version", ""))), f"version 不是 x.y.z：{data.get('version')}"
    assert int(data.get("api", 1)) <= 1, "api 版本比主程序新"


def test_entry_exposes_setup():
    entry = ROOT / str(manifest().get("entry", "plugin.py"))
    assert entry.exists(), f"缺少入口文件 {entry.name}"
    tree = ast.parse(entry.read_text(encoding="utf-8"))
    assert any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "setup"
        for node in tree.body
    ), "入口没有 setup(ctx)"


def test_panel_page_exists():
    panel = manifest().get("panel")
    if not panel:
        return
    assert (ROOT / "pages" / "index.html").exists(), "声明了 panel 却没有 pages/index.html"


def test_only_sdk_imports():
    for path in python_files():
        for module in imported_modules(path.read_text(encoding="utf-8")):
            if module.split(".")[0] != "app":
                continue
            assert module == "app.sdk" or module.startswith("app.sdk."), (
                f"{path.relative_to(ROOT)} 引了 {module}，插件只能 import app.sdk"
            )


def test_no_hardcoded_absolute_paths():
    """发布的代码里别写死路径（tests/ 是开发用的，不扫）。"""
    pattern = re.compile(r"[A-Za-z]:\\\\|[A-Za-z]:\\[^\"']*\\|/home/|/Users/")
    for path in python_files():
        rel = path.relative_to(ROOT)
        if rel.parts[0] == "tests":
            continue
        assert not pattern.search(path.read_text(encoding="utf-8")), f"{rel} 里写死了绝对路径"
