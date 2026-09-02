"""跨域默认值唯一归宿（P1 叶子层）。

抓包默认端口/目录是三方共同需要的配置面知识（mpconf 的 DEFAULT_CONFIG
schema 默认、sysctl 的系统代理指向、capture 自身的目录准备）。此前定义在
capture_store，迫使 mpconf 与 sysctl 横向 import 整个 capture 域——现在
常量归叶子层，域间边严格向下（见 tests/test_arch_imports.py）。

capture_store 仍持有抓包命名知识的其余部分（文件名格式、目录管理），
并从这里消费默认值。
"""
import os

DEFAULT_CAPTURE_DIR = os.path.expanduser("~/.magic-proxy-captures")
DEFAULT_CAPTURE_PORT = 8080
