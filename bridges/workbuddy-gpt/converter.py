#!/usr/bin/env python3
"""workbuddy-gpt 桥入口（包装脚本）。

两座 WorkBuddy 桥（国内 copilot.tencent.com / 海外 www.workbuddy.ai）的实现
完全共用 bridges/workbuddy/core.py，差异只有 WORKBUDDY_PROVIDER 一处；本文件
只负责把它指到正确变体并把共享目录接进 sys.path。

状态（auths/、bridge-settings.json、assets/、VERSION）仍留在本桥自己的目录，
由 WORKBUDDY_BRIDGE_DIR 指定，绝不落到共享目录里。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.join(os.path.dirname(_HERE), "workbuddy")

os.environ.setdefault("WORKBUDDY_PROVIDER", "gpt")
os.environ.setdefault("WORKBUDDY_BRIDGE_DIR", _HERE)

if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

from core import main

if __name__ == "__main__":
    main()

