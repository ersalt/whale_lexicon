"""core/ 的单元测试：不装机器人本体也能跑。

    python -m pytest tests -q      # 用 qq-bot-lite 的 venv，pytest 已经装好了
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.miner import is_low_value, parse_words, resolve_thinking  # noqa: E402
from core.service import SlangError, SlangService  # noqa: E402
from core.store import JargonStore  # noqa: E402
from tools.import_from_astrbot import map_scope  # noqa: E402

# ---------- 作用域转换（从 AstrBot 搬数据用） ----------


def test_map_scope():
    assert map_scope("") == ""
    assert map_scope("123456789") == "group:123456789"
    assert map_scope("group:42") == "group:42"
    assert map_scope("大肥鱼:GroupMessage:123") == "group:123"
    assert map_scope("大肥鱼:FriendMessage:456") == "private:456"


# ---------- 关思考参数 ----------


def test_resolve_thinking_modes():
    assert resolve_thinking("off", "", "") == {}
    assert resolve_thinking("openai", "", "") == {"reasoning_effort": "none"}
    assert resolve_thinking("deepseek", "", "") == {"reasoning_effort": "none"}
    assert resolve_thinking("glm", "", "") == {"thinking": {"type": "disabled"}}
    assert resolve_thinking("gemini", "", "") == {"gm_thinking_config": {"budget": 0}}
    assert resolve_thinking("custom", '{"temperature": 0}', "") == {"temperature": 0}
    # auto：按模型名猜
    assert resolve_thinking("auto", "", "glm-4-flash") == {"thinking": {"type": "disabled"}}
    assert resolve_thinking("auto", "", "gemini-2.0-flash") == {"gm_thinking_config": {"budget": 0}}
    assert resolve_thinking("auto", "", "qwen3") == {"reasoning_effort": "none"}
    # 坏 JSON 不炸，返回空
    assert resolve_thinking("custom", "{不是 json", "") == {}


def test_is_low_value():
    assert is_low_value("小鲸鱼", "群友 小鲸鱼 的 ID")
    assert is_low_value("小鲸鱼", "小鲸鱼 的 QQ 号")
    assert not is_low_value("石锤", "确凿的证据")
    # 带解释的不是同义反复
    assert not is_low_value("石锤", "石锤：确凿的证据")


def test_parse_words():
    assert parse_words("摸鱼,牢c，测试") == {"摸鱼", "牢c", "测试"}
    assert parse_words("") == set()


# ---------- 词库增删审 ----------


def _service(tmp_path: Path) -> SlangService:
    return SlangService(JargonStore(str(tmp_path / "jargon.db")))


def test_manual_and_pending_roundtrip(tmp_path: Path):
    svc = _service(tmp_path)
    svc.add_manual("梁文锋", "DeepSeek 创始人", scope="")
    svc.add_manual("帕区", "帕拉丁的简称", scope="group:1", keywords="帕拉丁")
    # 同名同作用域不允许重复录入
    try:
        svc.add_manual("梁文锋", "另一条", scope="")
    except SlangError:
        pass
    else:
        raise AssertionError("重名应该报错")

    svc.submit("兼职", "转职或多重职业", scope="group:1", auto_note="仅出现 1 次")
    total, items = svc.list_entries("pending")
    assert total == 1 and items[0]["content"] == "兼职"

    where = svc.approve("auto_pending", "兼职", "group:1")
    assert where == "approved"
    assert svc.stats()["pending"] == 0

    # 再驳回一次，回到待审
    assert svc.approve("auto", "兼职", "group:1") == "pending"
    assert svc.stats()["pending"] == 1

    # 作用域统计
    scopes = {row["scope"] for row in svc.scopes_of("auto_pending")}
    assert scopes == {"group:1"}

    # 批量删除
    result = svc.bulk("delete", [{"table": "auto_pending", "name": "兼职", "scope": "group:1"}])
    assert result["deleted"] == 1
    assert svc.stats()["pending"] == 0


def test_match_respects_scope(tmp_path: Path):
    svc = _service(tmp_path)
    svc.add_manual("石锤", "确凿的证据", scope="group:1")
    svc.add_manual("答辩", "论文答辩", scope="")

    assert svc.match("这是石锤", "group:1")
    assert not svc.match("这是石锤", "group:2")  # 别的群看不到
    assert svc.match("又要答辩", "group:2")  # 全局的哪都能用
