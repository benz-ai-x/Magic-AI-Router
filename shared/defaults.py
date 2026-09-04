"""跨域默认值唯一归宿（P1 叶子层）。

抓包默认端口/目录是多方共同需要的配置面知识（mpconf 的 DEFAULT_CONFIG
schema 默认、sysctl 的系统代理指向、capture 的目录准备、app 的菜单回
调）。网关默认端口同样跨 suanpan schema / sp_config 兜底 / lifecycle
端口探测三方出现。此前这些字面量散布各域（抓包侧曾在 capture_store，
迫使 mpconf 与 sysctl 横向 import 整个 capture 域）——现在常量归叶子
层，域间边严格向下（见 tests/test_arch_imports.py）。

capture_store 仍持有抓包命名知识的其余部分（文件名格式、目录管理），
并从这里消费默认值。
"""
import os

DEFAULT_CAPTURE_DIR = os.path.expanduser("~/.magic-proxy-captures")
DEFAULT_CAPTURE_PORT = 8080
DEFAULT_GATEWAY_PORT = 9527
