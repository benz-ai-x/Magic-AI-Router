"""稳定 id 迁移的可行动错误——跨域共享语义（P2 归位）。

mpconf（隧道 id，issue #8）与 suanpan（provider id）各自做确定性 id
赋值，重复身份/重复 id 都属于「配置身份无法唯一归属」这一同类可行动
错误：绝不与文件损坏混同（不触发 .bak 隔离），原样上抛。此前类定义
住在 mpconf/config，suanpan 只能横向借用（且以文件底部延迟导入规避
模块级耦合）——归位叶子层后两个域各自向下 import。
"""


class IdentityMigrationError(ValueError):
    """稳定 id 迁移的可行动错误（重复身份/重复 id）——绝不与文件损坏
    混同：不触发 .bak 隔离，原样上抛（issue #8）。"""
