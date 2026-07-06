@echo off
:: DE Accountant Agent One-Click Start Script for Windows
:: Usage: Just double-click this file or run "start.bat" in cmd

echo ===========================================
echo 🚀 Starting Accountant Agent locally...
echo ===========================================

:: 1. Check for Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ❌ Error: Python is not installed or not in your PATH.
    echo Please install Python 3.11+ and try again.
    pause
    exit /b 1
)

:: 2. Check for .env file
if not exist .env (
    echo 📝 First-time setup: .env file not found. Copying .env.example...
    copy .env.example .env >nul
    
    echo ==========================================================
    echo 🔑 Let's configure your DeepSeek API Key.
    echo    Please paste your Key below and press Enter:
    echo ==========================================================
    set /p USER_KEY="API Key: "
    
    if not "%USER_KEY%"=="" (
        :: Simple replace placeholder in .env on Windows
        powershell -Command "(gc .env) -replace 'DEEPSEEK_API_KEY=.*', 'DEEPSEEK_API_KEY=%USER_KEY%' | Out-File -encoding UTF8 .env"
        echo ✅ DeepSeek API Key successfully configured in .env.
    ) else (
        echo ⚠️ No key entered. Remember to edit .env manually before running.
    )
)

:: 3. Create virtual environment if not exists
if not exist venv (
    echo 📦 Creating virtual environment (venv)...
    python -m venv venv
)

:: 4. Activate virtual environment
echo 🔌 Activating virtual environment...
call venv\Scripts\activate.bat

:: 5. Install/update dependencies
echo 📥 Installing/updating dependencies from requirements.txt...
python -m pip install --upgrade pip
pip install -r requirements.txt

:: 6. Create directories
if not exist data mkdir data
if not exist data\config_history mkdir data\config_history

:: 7. Start FastAPI app
echo 🔥 Starting FastAPI server on port 8080...
echo 💡 Web Console URL: http://localhost:8080
uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload

pause
