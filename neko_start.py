"""启动引导脚本（给 启动蕴.vbs 用）。

为什么要单独一个文件
--------------------
Windows 快捷方式/脚本里用 `pythonw -m neko.app` 启动有两层坑：

1. `-m` 依赖**当前工作目录**能找到 `neko` 包。而
   `WScript.Shell.CurrentDirectory` 不保证传给子进程 —— 找不到包时
   pythonw 没有控制台，会**静默失败**（表现为"双击了但什么都没发生"）。
2. 绕一圈用 .bat 去 `cd` 又会引入新的坑：cmd 读取批处理文件的代码页在启动时
   就固定了，含非 ASCII 内容就可能在别的代码页下解析失败；
   而且隐藏窗口运行批处理还出过"启动了但没反应"的情况。

所以这里直接给 pythonw 一个**绝对路径的脚本**：自己把项目根目录塞进
`sys.path`，自己切好工作目录，然后调用应用主入口。
不再依赖 cwd，也不再经过 cmd 那一层。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 切到项目根目录，保证 web/、data/、config.json 这些相对路径都能找到
try:
    os.chdir(ROOT)
except OSError:
    pass

from neko.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
