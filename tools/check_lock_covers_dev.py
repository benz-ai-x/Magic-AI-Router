"""CI 门禁（issue #14）：lock 覆盖 dev 声明的包名集合。

语义修正：只在 requirements-dev.txt 声明的包名变化时报错，不强制
每次 CI 解析到上游最新（版本由 lock 自身记录确定性版本——否则 CI
会随上游发版随机失败，实测 36 行版本差）。
"""
import re
import sys


def _names(text):
    return {re.match(r"^([a-zA-Z0-9._-]+)", line).group(1).lower()
            for line in text.splitlines() if re.match(r"^[a-zA-Z0-9._-]+", line)}


def main():
    lock_names = {re.match(r"^([a-zA-Z0-9._-]+)==", line).group(1).lower()
                  for line in open("requirements-lock.txt").read().splitlines()
                  if re.match(r"^[a-zA-Z0-9._-]+==", line)}
    dev_names = _names(open("requirements-dev.txt").read())
    missing = sorted(dev_names - lock_names)
    if missing:
        sys.exit("requirements-lock.txt 缺失 dev 声明的包：" + str(missing))
    print(f"lock 覆盖 dev 声明的 {len(dev_names)} 个包——无漂移")


if __name__ == "__main__":
    main()
