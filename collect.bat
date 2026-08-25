@echo off
rem 日次パイプラインのローカル実行ランチャー。
rem 中身は src/daily.py に集約してあり、GitHub Actions でも同じものを呼んでいる。
rem
rem 注意: このファイルは必ず CRLF 改行で保存すること。
rem       LF だけだと cmd.exe が解析に失敗し、何も実行しないまま 0 を返す。
setlocal
chcp 65001 > nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

set PY=C:\Python314\python.exe
if not exist "%PY%" set PY=python

"%PY%" "%~dp0src\daily.py" %*
exit /b %ERRORLEVEL%
