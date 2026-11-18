@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem  jqe local board (engine/tools/play.py)
rem  Two Windows-specific things the upstream docs do not mention:
rem    1) the referee library must be built as build\bin\jieqi_rules.dll
rem    2) play.py looks for a Linux .so by default -> set the env var
rem  Also needs Python 3.11+ ; 3.8 has no enum.StrEnum
rem ---------------------------------------------------------------

set "JIEQI_NATIVE_LIBRARY=%~dp0build\bin\jieqi_rules.dll"
if not exist "%JIEQI_NATIVE_LIBRARY%" goto no_dll

set "PY="
rem 1) an explicit override wins  2) the py launcher  3) whatever is on PATH
if defined JIEQI_PY (
  "%JIEQI_PY%" -c "import sys;sys.exit(sys.hexversion<0x030B0000)" >nul 2>nul
  if not errorlevel 1 set "PY=%JIEQI_PY%"
)
for %%P in ("py -3.13" "py -3.12" "py -3.11" "py -3" "python" "python3") do (
  if not defined PY (
    %%~P -c "import sys;sys.exit(sys.hexversion<0x030B0000)" >nul 2>nul
    if not errorlevel 1 set "PY=%%~P"
  )
)
if not defined PY goto no_py

set "ENGINE=%~dp0engine\build-manual\jqe.exe"
set "NET=%~dp0engine\nets\it014.jqn"
if not exist "%ENGINE%" goto no_engine
if not exist "%NET%" goto no_net

echo interpreter : %PY%
echo rules lib   : %JIEQI_NATIVE_LIBRARY%
echo engine      : %ENGINE%
echo net         : %NET%
echo.
echo open http://localhost:8765 in your browser -- press Ctrl+C here to stop
echo.

"%PY%" engine\tools\play.py --port 8765 --engine "%ENGINE%" --net "%NET%"
echo.
echo server exited.
pause
exit /b 0

:no_dll
echo [x] missing referee library: %JIEQI_NATIVE_LIBRARY%
echo     build it first (MSYS2 UCRT64 g++):
echo       cd /d "%~dp0native"
echo       g++ -std=c++20 -O2 -shared -DJIEQI_BUILD_DLL -I include -static -static-libgcc -static-libstdc++ -o ..\build\bin\jieqi_rules.dll c_api.cpp rules.cpp repetition.cpp chase.cpp
echo.
pause
exit /b 1

:no_py
echo [x] need Python 3.11 or newer (play.py uses enum.StrEnum).
echo     install it, or set JIEQI_PY to a python.exe:
echo       set JIEQI_PY=C:\Python311\python.exe
echo.
pause
exit /b 1

:no_engine
echo [x] missing engine: %ENGINE%
echo     build it with the g++ line in engine\README.md
echo.
pause
exit /b 1

:no_net
echo [x] missing network: %NET%
pause
exit /b 1
