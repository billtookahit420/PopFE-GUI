@echo off
rem Launch Pop-fe GUI on Windows. On first run, create the
rem bundled virtual environment and install the app's deps.
rem NOTE: the GUI itself runs on Windows, but pop-fe's own
rem Windows behaviour is upstream's domain and this build is
rem verified on Linux -- see README.md.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo First run: setting up the Python environment...
    python -m venv .venv || (
        echo Could not create the environment. Install Python 3.10+
        echo from python.org, tick "Add python.exe to PATH", retry.
        exit /b 1
    )
)
.venv\Scripts\python.exe -c "import PySide6, PIL" 2>nul
if errorlevel 1 (
    echo Installing Python dependencies...
    .venv\Scripts\python.exe -m pip install -r requirements.txt || (
        echo Dependency install failed - check connection, re-run.
        exit /b 1
    )
)
.venv\Scripts\python.exe app\main.py %*
