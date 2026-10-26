@echo off
rem Запуск панели Discord Parser под Windows
setlocal
cd /d "%~dp0"

if not exist ".venv" (
  echo [1/2] Создаю venv и ставлю зависимости...
  python -m venv .venv || goto :error
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :error
)

if "%HOST%"=="" set HOST=127.0.0.1
if "%PORT%"=="" set PORT=8000

echo.
echo Discord Parser: http://%HOST%:%PORT%
echo Остановить: Ctrl+C
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host %HOST% --port %PORT% %*
goto :eof

:error
echo.
echo Не удалось установить зависимости. Проверьте, что установлен Python 3.11+ и есть интернет.
pause
