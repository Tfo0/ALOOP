"""日志: 同时输出到终端和 workspace/loop.log。"""

import pathlib
import time

_LOG_PATH: pathlib.Path | None = None   # init_log() 里设为 workspace/loop.log


def init_log(path: pathlib.Path) -> None:
    """设置日志文件位置, main() 启动时调一次。"""
    global _LOG_PATH
    _LOG_PATH = path


def log(msg: str) -> None:
    """同时输出到终端和 loop.log。flush 保证实时可见。"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _LOG_PATH is not None:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
