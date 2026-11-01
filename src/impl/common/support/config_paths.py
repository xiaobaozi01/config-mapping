"""从源码安装位置定位项目配置目录。"""

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[4]


def config_path(*parts: str) -> Path:
    """返回项目配置目录下的指定路径。

    路径从源码安装位置解析，使规则和镜像文件不依赖调用方的当前工作目录。
    """
    return (_PROJECT_ROOT / "config").joinpath(*parts)
