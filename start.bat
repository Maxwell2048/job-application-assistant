@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title 求职 RAG

echo ==========================================
echo   求职 RAG  -  本机启动
echo ==========================================

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] 首次运行：创建虚拟环境并安装依赖（需要网络，几分钟）...
    py -3 -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -r requirements-backend.txt || goto :fail
) else (
    echo [1/4] 虚拟环境已存在。
)

if not exist "config.local.json" (
    echo [!] 缺少 config.local.json。请复制 config.local.example.json 为 config.local.json 并填写本机模型服务地址。
    goto :fail
)
if not exist "resume\base_resume.docx" (
    echo [!] 缺少 resume\base_resume.docx（简历底稿）。把底稿放到 resume\ 目录后再启动。
    goto :fail
)

echo [2/4] 前端...
if exist "frontend\dist\index.html" (
    echo    已有构建产物 frontend\dist。
) else (
    where npm >nul 2>&1
    if errorlevel 1 (
        echo [!] 没有 frontend\dist，也没有安装 Node.js；页面将只显示接口文档。安装 Node 22 后运行 frontend 里的 npm install 和 npm run build。
    ) else (
        echo    首次构建前端（需要网络，约一分钟）...
        pushd frontend
        if not exist "node_modules" call npm install --no-fund --no-audit || (popd & goto :fail)
        call npm run build || (popd & goto :fail)
        popd
    )
)

echo [3/4] 数据库迁移...
pushd backend
"..\.venv\Scripts\python.exe" -X utf8 -m alembic upgrade head >nul 2>&1 || echo    迁移未执行（首次启动会自动建表）。
popd

echo [4/4] 启动后端（会打开一个标题为"求职 RAG 服务"的窗口，关闭它即停止）
start "求职 RAG 服务" ".venv\Scripts\python.exe" -X utf8 -m backend.app.main --port 8000
timeout /t 4 /nobreak >nul
start "" "http://127.0.0.1:8000"
echo 已在浏览器打开 http://127.0.0.1:8000 。模型离线时点左栏"启动"，或等自动处理自行启动。本窗口可以关闭。
timeout /t 6 >nul
goto :eof

:fail
echo.
echo 启动失败，见上方提示。
pause
