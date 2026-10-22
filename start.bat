@echo off
chcp 65001 >nul
title Python 一键启动

rem ====== 切换到脚本所在目录（bat 放哪，就从哪运行）======
cd /d "%~dp0"

rem ====== 如果有虚拟环境（venv 文件夹），自动激活；没有就跳过 ======
if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)

rem ====== 改成你要运行的 Python 文件名 ======
set SCRIPT=app.py

rem ====== 运行 ======
python "%SCRIPT%"

rem ====== 运行结束后窗口停留，方便看输出 / 报错 ======
echo.
echo ===== 程序已结束，按任意键关闭窗口 =====
pause >nul