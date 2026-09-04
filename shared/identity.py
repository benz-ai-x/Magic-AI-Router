"""稳定 id 的跨域契约——mpconf（隧道）与 suanpan（provider）共用。

两个域各自做确定性 id 赋值，重复身份/重复 id 都属于「配置身份无法
唯一归属」这一同类可行动错误：绝不与文件损坏混同（不触发 .bak
隔离），原样上抛。此前类定义住在 mpconf/config，suanpan 只能横向
借用（且以文件底部延迟导入规避模块级耦合）——归位叶子层后两个域
各自向下 import。

id 派生（stable_id）同样两域各自手写（t-/p- 前缀 + 同一 sha1 截断），
P2 归位时只搬了异常类——现补齐：截断长度与编码是跨域契约，改任一
常量会让已落盘 id 与重派生值失配（api_key keep/replace 语义断裂）。
"""
import hashlib


def stable_id(prefix: str, basis: str) -> str:
    """确定性 id：``<prefix>-<sha1(basis)[:10]>``——同 basis 恒同 id（issue #8）。"""
    return f"{prefix}-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]


class IdentityMigrationError(ValueError):
    """稳定 id 迁移的可行动错误（重复身份/重复 id）——绝不与文件损坏
    混同：不触发 .bak 隔离，原样上抛（issue #8）。"""
