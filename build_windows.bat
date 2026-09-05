@echo off
setlocal
cd /d "%~dp0"
title Market - EXE ve Setup
if not exist "main.py" (
 echo HATA: main.py bulunamadi.
 pause
 exit /b 1
)
echo [1/4] Python kontrol ediliyor...
python --version
if errorlevel 1 (
 echo Python 3.11 veya 3.12 kurulu olmali.
 pause
 exit /b 1
)
echo [2/4] Paketler kuruluyor...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
 echo Paket kurulumu basarisiz.
 pause
 exit /b 1
)
echo [3/4] Market.exe olusturuluyor...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
python -m PyInstaller --noconfirm --clean --windowed --onedir --name Market main.py
if errorlevel 1 (
 echo EXE olusturulamadi.
 pause
 exit /b 1
)
echo [4/4] Setup olusturuluyor...
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
 echo Inno Setup 6 bulunamadi.
 echo Once su programi kur: https://jrsoftware.org/isinfo.php
 echo Sonra build_windows.bat dosyasini tekrar calistir.
 pause
 exit /b 0
)
if not exist installer mkdir installer
"%ISCC%" installer.iss
if errorlevel 1 (
 echo Setup olusturulamadi.
 pause
 exit /b 1
)
echo.
echo TAMAMLANDI!
echo Setup dosyasi: %CD%\installer\MarketSetup.exe
echo.
pause
