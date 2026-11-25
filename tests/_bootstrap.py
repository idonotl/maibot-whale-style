"""测试用的插件包引导。

插件目录名 ``whale-style`` 含连字符，不能直接 ``import``；生产环境里 Runner 以合成包名
加载 ``plugin.py``，包内 ``from .store import SCENES`` 因此可用。测试复现同样的包结构，
让被测模块保持与运行时一致的相对导入。
"""

from __future__ import annotations

import importlib.util
import sys
import sysconfig
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "whale_style_under_test"

# 与 SDK / Runner 一致的第三方依赖路径，未安装 SDK 时才需要显式提供。
SDK_PATH = Path(sysconfig.get_paths()["purelib"])


def install_package() -> str:
    if PACKAGE_NAME in sys.modules:
        return PACKAGE_NAME

    if SDK_PATH.is_dir() and str(SDK_PATH) not in sys.path:
        sys.path.append(str(SDK_PATH))

    spec = importlib.util.spec_from_loader(PACKAGE_NAME, loader=None, is_package=True)
    if spec is None:
        raise RuntimeError("无法为插件目录创建包 spec")

    package = importlib.util.module_from_spec(spec)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PACKAGE_NAME] = package
    return PACKAGE_NAME


install_package()
