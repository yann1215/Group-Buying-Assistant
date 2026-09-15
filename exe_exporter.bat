@echo off
setlocal enabledelayedexpansion

cd /d D:\1_PychamProjects\Group-Buying-Assistant

echo ================================
echo 进入项目目录
echo ================================

echo 当前目录：
cd

echo.
echo ================================
echo 更新子模块
echo ================================
git submodule update --init --recursive
if errorlevel 1 goto error

echo.
echo ================================
echo 激活虚拟环境
echo ================================
call .\.venv\Scripts\activate.bat
if errorlevel 1 goto error

echo.
echo 当前 Python：
python -c "import sys; print(sys.executable)"
if errorlevel 1 goto error

echo.
echo ================================
echo 安装依赖
echo ================================
python -m pip install -r requirements.txt
if errorlevel 1 goto error

echo.
echo ================================
echo 检查主程序语法
echo ================================
python -m py_compile main_gui.py
if errorlevel 1 goto error

echo.
echo ================================
echo 删除旧 build / dist
echo ================================
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo ================================
echo 开始 PyInstaller 打包
echo ================================
python -m PyInstaller --noconfirm --clean group_buying_assistant.spec
if errorlevel 1 goto error

echo.
echo ================================
echo 创建运行目录
echo ================================
if not exist "dist\GroupBuyingAssistant\orders" mkdir "dist\GroupBuyingAssistant\orders"
if not exist "dist\GroupBuyingAssistant\orders\output" mkdir "dist\GroupBuyingAssistant\orders\output"
if not exist "dist\GroupBuyingAssistant\temp" mkdir "dist\GroupBuyingAssistant\temp"
if not exist "dist\GroupBuyingAssistant\logs" mkdir "dist\GroupBuyingAssistant\logs"
if not exist "dist\GroupBuyingAssistant\data" mkdir "dist\GroupBuyingAssistant\data"

echo.
echo ================================
echo 打包完成
echo ================================
echo 生成位置：
echo dist\GroupBuyingAssistant\GroupBuyingAssistant.exe
echo.
pause
exit /b 0

:error
echo.
echo ================================
echo 打包失败
echo ================================
echo 请查看上方报错信息。
echo.
pause
exit /b 1