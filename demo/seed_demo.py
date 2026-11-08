"""Create a demo owner home (fake data) for previewing the dashboard."""
import sqlite3
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
home, ws = root / "owner", root / "workspace"
(home / "memories").mkdir(parents=True, exist_ok=True)
(home / "skills/writing/blog-helper").mkdir(parents=True, exist_ok=True)
(ws / "自传").mkdir(parents=True, exist_ok=True)
(home / "memories/MEMORY.md").write_text("\n§\n".join([
    "博客：lowtide.news，写 AI agent 和独立开发，最近一篇《把 Hermes 部署成个人分身》",
    "硕士读的是计算机，方向是分布式系统",
    "（私密）上个月和家人吵架后心情很差，聊过很久",
    "（私密）服务器 vps-1 的备份放在 /srv/backups"]), encoding="utf-8")
(home / "memories/USER.md").write_text("\n§\n".join([
    "用户叫 .v，常用中文交流", "（私密）住在北京朝阳区"]), encoding="utf-8")
(home / "SOUL.md").write_text("You are Hermes, .v's personal agent.\n", encoding="utf-8")
(home / "skills/writing/blog-helper/SKILL.md").write_text("---\nname: blog-helper\n---\n帮 .v 写博客草稿。\n", encoding="utf-8")
(ws / "自传/自传.md").write_text("# 我的自传\n\n## 童年\n在南方小城长大。\n\n## 求学\n本科和硕士都读计算机。\n\n## 感情经历\n（私密）这一段只给自己看。\n", encoding="utf-8")
(ws / "notes.txt").write_text("（私密）随手记：下周去医院复查。\n", encoding="utf-8")
con = sqlite3.connect(home / "state.db")
con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL, message_count INTEGER)")
for i, t in enumerate(["聊自传的结构", "服务器升级 v0.22.2", "情绪低落的一晚"]):
    con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)", (f"s{i}", "dashboard", t, time.time() - i * 86400, 20 + i))
con.commit()
print(home, ws)
