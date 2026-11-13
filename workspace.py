"""workspace: 黑板目录布局 —— 系统的全部状态就是磁盘上这几个目录。

  queue/todo/   待办池: 一个文件 = 一条待办
  queue/doing/  进行中: loop 派发时把文件从 todo/ 挪到这里 (= 认领)
  cache/        缓存库: agent 沉淀的关键发现, 按主题子目录归类; done/ 子目录存做完的待办卡片
  goal/         成果目录: 验收只看这里

删文件 = 打回重做, 改文件 = 人工提示, 重启程序 = 断点续跑。
"""

import pathlib


class Workspace:
    """一个 workspace 目录下的全部已知路径。"""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self.queue_todo = root / "queue" / "todo"
        self.queue_doing = root / "queue" / "doing"
        self.cache = root / "cache"
        self.cache_done = self.cache / "done"
        self.goal = root / "goal"
        self.seq_file = root / "queue" / ".seq"   # 卡片递增序号的计数器

    def ensure(self) -> None:
        """首次运行自动建目录。"""
        for d in (self.queue_todo, self.queue_doing,
                  self.cache, self.goal):
            d.mkdir(parents=True, exist_ok=True)
