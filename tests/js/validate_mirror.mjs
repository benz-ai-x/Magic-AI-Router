// 跨语言校验漂移报警的 JS 半边（架构评审 R2-3）：stdin 喂 {mp, sp}
// 原始保存候选，跑设置窗 LAYER 1 的真实 validateConfig（extract.mjs
// 取 HTML 内嵌实现——钉住的是「随包发布的那份」，不是副本），
// stdout 输出错误行 JSON。由 tests/test_validation_mirror.py 驱动：
// 同一语料两侧同跑，镜像族断言「同错同净」，单侧族显式白名单。
import { readFileSync } from "node:fs";
import { loadLayer } from "./extract.mjs";

const input = JSON.parse(readFileSync(0, "utf8"));
const L = loadLayer(1);
const S = L.normalizeState(input);
process.stdout.write(JSON.stringify(L.validateConfig(S)));
