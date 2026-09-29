"""Initialize the local PostgreSQL cluster without a machine-wide installer."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile


root = Path(__file__).resolve().parents[1]
initdb = root / ".postgres-server" / "pgsql" / "bin" / "initdb.exe"
data = Path(os.environ["LOCALAPPDATA"]) / "LAPIS" / "postgres-data"
secret_file = root / ".postgres-server" / "superuser.json"
if secret_file.exists() or (data.exists() and any(data.iterdir())):
    raise SystemExit("已有 PostgreSQL 数据或凭据，拒绝重新初始化")
if not initdb.exists():
    raise SystemExit("找不到 initdb.exe；先解压官方 Windows 二进制包")
data.mkdir(parents=True, exist_ok=True)
password = secrets.token_urlsafe(32)
with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as stream:
    stream.write(password + "\n")
    password_file = Path(stream.name)
try:
    subprocess.run([str(initdb), "-D", str(data), "--username=postgres",
                    f"--pwfile={password_file}", "--auth-host=scram-sha-256",
                    "--auth-local=scram-sha-256", "--encoding=UTF8", "--locale=C"], check=True)
finally:
    password_file.unlink(missing_ok=True)
secret_file.write_text(json.dumps({"password": password}), encoding="utf-8")
print("PostgreSQL 数据目录已初始化；超级用户凭据仅保存在忽略的文件中")
