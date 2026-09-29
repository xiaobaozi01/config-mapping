"""支持通过 ``python -m config_adaptor`` 启动命令行程序。"""

from .cli import main

# 将 CLI 的返回码原样交给操作系统，便于脚本判断转换是否成功。
raise SystemExit(main())
