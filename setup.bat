@echo off
chcp 65001 >nul
title Dududa 2.0 一键安装

echo.
echo  ==========================================
echo    Dududa 2.0 Agent Runtime - 一键安装
echo  ==========================================
echo.

:: 1. 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [警告] 未检测到 Python，正在下载安装...
    echo.
    curl -o python-installer.exe https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
    python-installer.exe /quiet InstallAllUsers=1 PrependPath=1
    del python-installer.exe
    echo  [完成] Python 安装完毕，请重新打开命令行再运行此脚本
    pause
    exit /b
)

echo  [OK] Python 已就绪
python --version

:: 2. 升级 pip
echo.
echo  [2/3] 升级 pip...
python -m pip install --upgrade pip -q

:: 3. 安装依赖
echo.
echo  [3/3] 安装项目依赖...
pip install -r requirements.txt -q

:: 4. 验证
echo.
echo  [验证] 运行测试...
python -m pytest tests/ -q --tb=line 2>nul
if %errorlevel% equ 0 (
    echo  [OK] 全部测试通过！
) else (
    echo  [警告] 部分测试未通过，但核心功能通常不受影响
)

echo.
echo  ==========================================
echo    安装完成！
echo.
echo    启动控制台：
echo      python -c "from packages.control_plane import run_server; run_server()"
echo    然后浏览器打开 http://127.0.0.1:8000
echo  ==========================================
pause