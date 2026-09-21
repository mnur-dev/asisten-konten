@echo off
setlocal
title Asisten Konten - Catur
cd /d "%~dp0"

set URL=http://127.0.0.1:8420
set PYTHON=.venv\Scripts\python.exe

rem Sudah jalan? Langsung buka browser saja, jangan buka server kedua.
netstat -ano | findstr /r /c:"127.0.0.1:8420.*LISTENING" >nul
if not errorlevel 1 (
    echo Aplikasi sudah berjalan, membuka browser...
    start "" "%URL%"
    goto :eof
)

if not exist "%PYTHON%" (
    echo [ERROR] Tidak menemukan %PYTHON%
    echo Pastikan virtual environment sudah dibuat ^(lihat CLAUDE.md^).
    pause
    goto :eof
)

echo Menjalankan Asisten Konten...
rem Browser dibuka oleh .bat ini di bawah; jangan biarkan server buka tab kedua.
set ASISTEN_NO_BROWSER=1
start "Asisten Konten - Server (jangan ditutup)" /min "%PYTHON%" -m app

echo Menunggu server siap...
set /a tries=0
:waitloop
set /a tries+=1
if %tries% gtr 30 (
    echo [ERROR] Server tidak merespons setelah 30 detik. Cek jendela server yang diminimize.
    pause
    goto :eof
)
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr /r /c:"127.0.0.1:8420.*LISTENING" >nul
if errorlevel 1 goto waitloop

start "" "%URL%"
