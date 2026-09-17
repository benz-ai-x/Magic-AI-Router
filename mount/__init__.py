"""mount 域 —— NFSv4 over SSH 隧道挂载（ADR-007）。

远程侧一键安装（remote_setup）+ 本地挂载控制（mount_control）+
专用 NFS 转发会话（nfs_session）+ 挂载生命周期协调（coordinator）。

ssh 调用策略单一归宿在 tunnel/ssh_launch（架构守卫白名单边
("mount", "tunnel")），本域不自行构造 ssh argv。
"""
