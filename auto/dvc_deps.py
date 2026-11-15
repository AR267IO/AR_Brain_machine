# -*- coding: utf-8 -*-
r"""
运行库检查
==========
在 import numpy / PIL 之前先跑一遍，缺什么就用中文说清楚，
并且直接给出可以复制粘贴的安装命令，而不是甩一段 ModuleNotFoundError 的 traceback。
"""
import importlib
import sys

# (import 名, pip 包名)
_REQUIRED = (
    ("numpy", "numpy"),
    ("PIL", "pillow"),
)

LINE = "-" * 60


def missing(tk=False):
    """返回缺少的 pip 包名列表；tk=True 时连 tkinter 一起查。"""
    miss = []
    for mod, pkg in _REQUIRED:
        try:
            importlib.import_module(mod)
        except Exception:
            miss.append(pkg)
    if tk:
        try:
            importlib.import_module("tkinter")
        except Exception:
            miss.append("tkinter")
    return miss


def require(script=None, tk=False, quiet=False):
    """缺库就打印人话并退出（退出码 1）；齐全则直接返回。"""
    miss = missing(tk)
    if not miss:
        return

    print(LINE)
    print("缺少运行库：%s" % "、".join(miss))
    print(LINE)

    if "tkinter" in miss:
        print("tkinter 是 Python 自带的，缺了说明你装的是精简版或免安装压缩包。")
        print("请去 python.org 下官方安装包重装一次，安装界面保持默认勾选即可。")
        print("")

    pips = [m for m in miss if m != "tkinter"]
    if pips:
        print("在当前这个窗口里直接粘贴执行：")
        print("")
        print("    python -m pip install %s" % " ".join(pips))
        print("")
        print("如果下载很慢，换清华镜像：")
        print("")
        print("    python -m pip install %s -i https://pypi.tuna.tsinghua.edu.cn/simple"
              % " ".join(pips))
        print("")
        print("装完重新双击 run.bat 即可。")

    print(LINE)
    print("当前解释器：%s" % sys.executable)
    print("Python 版本：%s" % sys.version.split()[0])
    if script:
        print("脚本：%s" % script)
    print("")

    if not quiet and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("按回车关闭。")
        except EOFError:
            pass
    sys.exit(1)


if __name__ == "__main__":
    m = missing(tk=True)
    if m:
        require(__file__, tk=True)
    else:
        print("运行库齐全。")
        print("解释器：%s" % sys.executable)
