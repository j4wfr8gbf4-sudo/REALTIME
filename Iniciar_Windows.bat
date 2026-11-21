@echo off
rem El ZIP portable incluye el interprete en la carpeta runtime.
setlocal
title Seguimiento de Turnos
pushd "%~dp0" || exit /b 1
if not exist "runtime\python.exe" (
    echo Extrae el ZIP completo y vuelve a abrir Iniciar_Windows.bat.
    pause
    popd
    exit /b 1
)
"runtime\python.exe" -B "%CD%\app.py"
set "RESULTADO=%ERRORLEVEL%"
if not "%RESULTADO%"=="0" (
    echo.
    echo No se pudo iniciar Seguimiento de Turnos. Revisa el mensaje anterior.
    pause
)
popd
exit /b %RESULTADO%
