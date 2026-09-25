"""capture 厂商 host 字典漂移报警（架构评审 R5）。

capture/ai_capture_addon.CAPTURE_HOST_SUFFIXES 与
shared/provider_auth.PROVIDER_REGISTRY 的 hosts 刻意分叉：frozen mitmdump
子进程零仓内 import 是资源契约（ADR-001），抓包端点变体是 capture 域
知识——fork 本身是设计，但 fork 关系必须**显式声明**，不能静默漂移。

本测试是报警器（模式同 tests/test_validation_mirror.py 的白名单纪律）：
两侧关系全部钉进显式白名单，任何一侧新增厂商/host 而未挂号 → 红，迫使
「capture 是否识别该厂商」成为显式决策，而非既成事实。已声明的分叉：
- qwen：capture 多识别国际站 dashscope-intl；registry 的 maas 专属云入口
  capture 不抓；
- minimax：capture 走 API 端点域（minimaxi/minimax.chat），registry 只有
  公司域（minimax.io/minimax.cn）；
- 命名：capture 键 "doubao" ↔ registry 键 "volces"；
- glm / kimi / openrouter / siliconflow：registry 收录、capture 完全不识别。
"""
import unittest

from capture.ai_capture_addon import CAPTURE_HOST_SUFFIXES, match_provider
from shared.provider_auth import PROVIDER_REGISTRY

# ── 白名单：两侧分叉的显式声明（分叉消失即须摘除，见 hygiene 断言）──

# (b) capture 键 → registry 键（命名不同显式登记；未列出 = 同名）
CAPTURE_TO_REGISTRY_KEY = {"doubao": "volces"}

# (a) capture 识别、但该厂商 registry hosts 未收录的 host 后缀
#    （capture 域有意的额外端点，如国际站）
CAPTURE_EXTRA_HOSTS = {
    ("qwen", "dashscope-intl.aliyuncs.com"),  # 国际站；registry 仅中国站
    ("minimax", "api.minimaxi.com"),          # API 端点域；registry 只有公司域
    ("minimax", "api.minimax.chat"),
}

# 已捕获厂商名下、registry 收录但 capture 表无任何覆盖的 host
# （capture 有意的洞；新 host 默认要报警）
REGISTRY_HOST_UNCAPTURED = {
    ("qwen", "maas.aliyuncs.com"),  # 专属云入口，抓包端点未覆盖
    ("minimax", "minimax.cn"),      # 中国公司域（API 端点走 .com 家族）
}

# (c) registry 厂商 capture 完全不识别（显式决定：不抓）
UNCAPTURABLE_REGISTRY_VENDORS = {"glm", "kimi", "openrouter", "siliconflow"}


def _registry_key(capture_key):
    return CAPTURE_TO_REGISTRY_KEY.get(capture_key, capture_key)


def _same_family(a, b):
    """同一 host 家族：相等或互为子域（dot 边界，防 ev-il-suffix 伪亲缘）。"""
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _captured_registry_keys():
    return {_registry_key(k) for k in CAPTURE_HOST_SUFFIXES}


def _capture_suffixes_for(registry_key):
    return [s for k, suffixes in CAPTURE_HOST_SUFFIXES.items()
            if _registry_key(k) == registry_key for s in suffixes]


class TestCaptureHostMirror(unittest.TestCase):
    """两侧关系全声明：新增厂商/host 未挂号即红。"""

    maxDiff = None

    # ── 结构前提：键映射本身有效 ─────────────────────────

    def test_capture_keys_map_to_real_registry_vendors(self):
        """capture 每个键都必须落到真实 registry 厂商（映射表指向幽灵即红）。"""
        for capture_key in CAPTURE_HOST_SUFFIXES:
            rk = _registry_key(capture_key)
            self.assertIn(rk, PROVIDER_REGISTRY,
                          f"capture 厂商 {capture_key!r} 映射到 registry 不存在的"
                          f" {rk!r}——改 CAPTURE_TO_REGISTRY_KEY 或补 registry")

    def test_key_map_has_no_stale_entries(self):
        live = {k: _registry_key(k) for k in CAPTURE_HOST_SUFFIXES}
        for capture_key, rk in CAPTURE_TO_REGISTRY_KEY.items():
            self.assertEqual(live.get(capture_key), rk,
                             f"CAPTURE_TO_REGISTRY_KEY[{capture_key!r}] 已失效"
                             "（capture 表或 registry 已变）——请摘除")

    # ── (a) capture 侧：每个识别的后缀要么被 registry 解释、要么挂号 ──

    def test_capture_extra_hosts_are_whitelisted(self):
        for capture_key, suffixes in CAPTURE_HOST_SUFFIXES.items():
            rk = _registry_key(capture_key)
            reg_hosts = PROVIDER_REGISTRY[rk]["hosts"]
            for s in suffixes:
                if any(_same_family(s, r) for r in reg_hosts):
                    continue
                self.assertIn((capture_key, s), CAPTURE_EXTRA_HOSTS,
                              f"capture 识别 {capture_key!r} 的 {s!r} 但 registry"
                              f" [{rk}] hosts 未收录——新增端点请补 registry 或在"
                              " CAPTURE_EXTRA_HOSTS 挂号（并写明理由）")

    def test_capture_extra_whitelist_has_no_stale_entries(self):
        for capture_key, suffix in CAPTURE_EXTRA_HOSTS:
            self.assertIn(capture_key, CAPTURE_HOST_SUFFIXES,
                          f"CAPTURE_EXTRA_HOSTS 指向不存在的 capture 键"
                          f" {capture_key!r}")
            self.assertIn(suffix, CAPTURE_HOST_SUFFIXES[capture_key],
                          f"CAPTURE_EXTRA_HOSTS 的 ({capture_key!r}, {suffix!r})"
                          " 不在 capture 表中——已回归同族？请摘除")
            rk = _registry_key(capture_key)
            covered = any(_same_family(suffix, r)
                          for r in PROVIDER_REGISTRY[rk]["hosts"])
            self.assertFalse(covered,
                             f"({capture_key!r}, {suffix!r}) 已被 registry 解释"
                             "——不再是分叉，请摘除")

    # ── (c)+(d) registry 侧：每个厂商要么被 capture 覆盖、要么声明不抓 ──

    def test_registry_vendors_are_captured_or_declared_uncapturable(self):
        captured = _captured_registry_keys()
        for rk in PROVIDER_REGISTRY:
            if rk in captured:
                continue
            self.assertIn(rk, UNCAPTURABLE_REGISTRY_VENDORS,
                          f"新增 registry 厂商 {rk!r}：capture 是否识别须显式决定"
                          "——补 CAPTURE_HOST_SUFFIXES（含 identify 路径分发）或挂"
                          " UNCAPTURABLE_REGISTRY_VENDORS（写明不抓理由）")

    def test_uncapturable_whitelist_has_no_stale_entries(self):
        captured = _captured_registry_keys()
        for rk in UNCAPTURABLE_REGISTRY_VENDORS:
            self.assertIn(rk, PROVIDER_REGISTRY,
                          f"UNCAPTURABLE_REGISTRY_VENDORS 的 {rk!r} 不在 registry"
                          "——厂商已删？请摘除")
            self.assertNotIn(rk, captured,
                             f"{rk!r} 已被 capture 覆盖——不再是「不抓」，请摘除")

    def test_registry_hosts_of_captured_vendors_covered_or_declared(self):
        """已捕获厂商的每个 registry host 要么被 capture 后缀覆盖、要么挂号
        ——新增 host（新区域站/新入口）默认报警，迫使显式决定。"""
        for rk in _captured_registry_keys():
            suffixes = _capture_suffixes_for(rk)
            for r in PROVIDER_REGISTRY[rk]["hosts"]:
                if any(_same_family(r, s) for s in suffixes):
                    continue
                self.assertIn((rk, r), REGISTRY_HOST_UNCAPTURED,
                              f"registry [{rk}] 新增 host {r!r} 无 capture 后缀"
                              "覆盖——补 CAPTURE_HOST_SUFFIXES 或在"
                              " REGISTRY_HOST_UNCAPTURED 挂号（写明不抓理由）")

    def test_registry_uncaptured_whitelist_has_no_stale_entries(self):
        for rk, r in REGISTRY_HOST_UNCAPTURED:
            self.assertIn(rk, PROVIDER_REGISTRY,
                          f"REGISTRY_HOST_UNCAPTURED 的 {rk!r} 不在 registry"
                          "——请摘除")
            self.assertIn(r, PROVIDER_REGISTRY[rk]["hosts"],
                          f"REGISTRY_HOST_UNCAPTURED 的 ({rk!r}, {r!r}) 不是"
                          " registry host——请摘除")
            suffixes = _capture_suffixes_for(rk)
            covered = any(_same_family(r, s) for s in suffixes)
            self.assertFalse(covered,
                             f"({rk!r}, {r!r}) 已被 capture 覆盖——请摘除挂号")

    # ── 接线冒烟：表与 match_provider/identify 行为一致 ──

    def test_match_provider_agrees_with_table(self):
        """表化重构的接线断言：registry 覆盖面与 match_provider 逐 host 对账。"""
        for capture_key, suffixes in CAPTURE_HOST_SUFFIXES.items():
            for s in suffixes:
                # doubao 的代表 host 须含 ark（裸后缀 volces.com 是非 API 域）
                probe = f"ark.{s}" if capture_key == "doubao" else s
                self.assertEqual(match_provider(probe), capture_key,
                                 f"match_provider({probe!r}) 应识别为"
                                 f" {capture_key!r}")
        # doubao 的 ark 子域约束：非 ark 的 volces 子域放行
        self.assertEqual(match_provider("ark.cn-beijing.volces.com"), "doubao")
        self.assertIsNone(match_provider("console.volces.com"))
        # 挂号为「不抓」的 registry host 确实放行（白名单与行为互证）
        for rk, r in REGISTRY_HOST_UNCAPTURED:
            self.assertIsNone(match_provider(r),
                              f"({rk!r}, {r!r}) 挂号不抓但 match_provider 命中"
                              "——白名单已过期")
        for rk in UNCAPTURABLE_REGISTRY_VENDORS:
            for r in PROVIDER_REGISTRY[rk]["hosts"]:
                self.assertIsNone(match_provider(r),
                                  f"厂商 {rk!r} 挂号不抓但 host {r!r} 命中")


if __name__ == "__main__":
    unittest.main()
