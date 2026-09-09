@echo off
setlocal

cd /d "%~dp0"

echo [1/3] Updating main repository...
git pull

echo.
echo [2/3] Initializing submodules...
git submodule update --init --recursive

echo.
echo [3/3] Updating WeChatMsg_Lite to latest master...
cd external\WeChatMsg_Lite

git fetch origin
git checkout master

if errorlevel 1 (
    echo Failed to checkout master.
    pause
    exit /b 1
)

git pull origin master

if errorlevel 1 (
    echo Failed to update WeChatMsg_Lite.
    pause
    exit /b 1
)

cd ..\..

echo.
echo WeChatMsg_Lite updated successfully.
git status

pause