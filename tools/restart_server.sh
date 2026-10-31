#!/bin/bash
# 重启 PaperIDE 服务（**必须先杀干净**：两个进程能同时绑 8777，
# 请求随机落一个，表现就是"接口时好时坏"——已经踩过两次）
#
# 用哪个 Python：优先取环境变量 PYTHON，否则依次找 python3 / python。
set -u
cd "$(dirname "$0")/.." || exit 1

if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

PORT="${PORT:-8777}"

"$PY" -c "
import subprocess,sys,time
out=subprocess.run(['powershell','-NoProfile','-Command',
  \"Get-CimInstance Win32_Process -Filter \\\"Name='python.exe'\\\" | Where-Object {\$_.CommandLine -like '*server.py*'} | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }\"
],capture_output=True,text=True)
time.sleep(1.5)
"
PYTHONIOENCODING=utf-8 "$PY" server.py --port "$PORT" > /tmp/paperide-server.log 2>&1 &
sleep 4
curl -s --noproxy '*' "http://127.0.0.1:${PORT}/api/version"
