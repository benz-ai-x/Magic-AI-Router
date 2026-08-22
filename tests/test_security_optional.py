"""keychain 在 Security 框架缺失时仍可导入（Linux 容器形态）。"""


def test_keychain_importable_without_security_framework():
    import subprocess
    import sys
    code = (
        "import sys; sys.modules['Security'] = None;"
        "from sysctl import keychain;"
        "assert keychain.Security is None;"
        "t = {'ssh_host': 'h', 'ssh_user': 'u'};"
        "assert keychain.get_password(t) == '';"
        "assert keychain.set_password(t, 'x') is False;"
        "assert keychain.delete_password(t) is False;"
        "print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True,
                       cwd="/Users/pc2026/WorkSpace/DevTools/Magic-AI-Router")
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr
