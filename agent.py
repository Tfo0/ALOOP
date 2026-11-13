"""agent 调用层: 一次调用 = 一个全新 session 的独立 agent。"""

import asyncio
import os
import pathlib
import signal
import subprocess
import sys

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from log import log

_active_clients: set = set()   # 在跑的 CLI client, 退出时一锅端


def kill_all_agents() -> None:
    """Ctrl-C/验收退出时兜底: 把所有在跑的 CLI 进程树杀干净, 不留孤儿 claude 进程。
    只杀本 loop 自己拉起的, 不碰终端里别的 claude 实例。"""
    for client in list(_active_clients):
        _kill_tree(client)


async def heartbeat(queue_doing: pathlib.Path, goal_dir: pathlib.Path,
                    state: dict) -> None:
    """每 2 分钟打一行心跳: 几个 worker 在跑、在跑什么、goal/ 有几份了。
    agent 干活期间主流程是静默的, 没有心跳盯着看的人会以为死了。
    state["extra"] 由主循环设置: plan/watchdog 这类不占待办卡片的 agent 也报出来。"""
    while True:
        await asyncio.sleep(120)
        doing = [p.name for p in queue_doing.glob("*.md")]
        done = sum(1 for p in goal_dir.iterdir() if p.is_dir())  # 与验收口径一致: 数一级子目录
        extra = f" + {state['extra']}" if state.get("extra") else ""
        spawned = state.get("spawned")
        budget = f", 累计已派 {spawned} 个 agent" if spawned else ""
        log(f"心跳: {len(doing)} 个 worker 在跑 {doing}{extra}, goal/ 已有 {done} 份{budget}")


def _kill_tree(client: ClaudeSDKClient) -> None:
    """超时/异常后兜底: 拿到这次调用的 CLI 子进程 pid, 整棵树一起杀。
    SDK 的 close() 会终止 CLI 进程本身, 但 Windows 上不连带它拉起的
    node/cmd 子进程 —— 不杀干净的话, 每超时一次就留一堆孤儿进程。
    只杀本次调用自己的那棵树, 不碰别的实例。拿不到 pid 就算了(close 已尽力)。"""
    proc = getattr(getattr(client, "_transport", None), "_process", None)
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True)
        else:
            os.kill(pid, signal.SIGKILL)
            subprocess.run(["pkill", "-KILL", "-P", str(pid)], capture_output=True)
    except Exception:
        pass   # 进程可能已死, 尽力而为


async def run_agent(prompt: str, workspace: pathlib.Path, model: str,
                    timeout_minutes: int) -> str:
    """起一个 agent 在 workspace 里干活, 返回它的最终输出文本。

    每次调用都是零上下文的全新 session —— 它对世界的全部了解
    来自 prompt 和它自己用工具读到的目录内容。

    两种失败都只打日志、返回空串, 异常不外抛 —— 待办卡片留在 doing/,
    由 loop 收割时归档(中断的加 中断- 前缀), 长跑(过夜)场景一次抖动
    不该杀死整个 loop:
    - 超时: agent 挂住/跑太久 → wait_for 取消 + 关掉 CLI + taskkill 杀整棵进程树
    - 异常: 网络/SDK/CLI 错误(比如底层 node 进程崩溃) → 同样杀干净
    """
    options = ClaudeAgentOptions(
        model=model,                         # 本次任务用的模型, 不影响用户目录的全局配置
        # 这个 agent 能用哪些工具
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"],
        cwd=str(workspace),                  # 它的工作目录 = workspace
        permission_mode="bypassPermissions", # 全自动: 不逐条问权限(本地跑才这么干)
    )
    client = ClaudeSDKClient(options=options)
    _active_clients.add(client)
    try:
        async with client:
            await client.query(prompt)

            async def _collect() -> str:
                final = ""
                async for message in client.receive_response():
                    result = getattr(message, "result", None)  # 流式消息里只留最终结果
                    if result:
                        final = result
                return final

            return await asyncio.wait_for(
                _collect(), timeout=timeout_minutes * 60)
    except asyncio.TimeoutError:
        log(f"[agent 超时 {timeout_minutes} 分钟, 杀掉进程树]")
        _kill_tree(client)
        return ""
    except Exception as exc:
        log(f"[agent 异常, 杀掉进程树] {type(exc).__name__}: {exc}")
        _kill_tree(client)
        return ""
    finally:
        _active_clients.discard(client)
