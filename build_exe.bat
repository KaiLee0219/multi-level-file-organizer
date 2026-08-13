@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOG=%~dp0build_log.txt"
set "DIST=%~dp0dist"
set "BUILD=%~dp0build"
set "PYEXE="
set "PYARGS="

>"%LOG%" echo Multi-level Extractor build log
>>"%LOG%" echo Started: %date% %time%

where py >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=py"
  set "PYARGS=-3"
)
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE goto :no_python

"%PYEXE%" %PYARGS% --version >>"%LOG%" 2>&1
if errorlevel 1 goto :no_python

"%PYEXE%" %PYARGS% -m PyInstaller --version >>"%LOG%" 2>&1
if errorlevel 1 (
  echo Installing PyInstaller...
  "%PYEXE%" %PYARGS% -m pip install --upgrade pyinstaller >>"%LOG%" 2>&1
  if errorlevel 1 goto :install_failed
)

echo Building EXE, please wait...
"%PYEXE%" %PYARGS% -m PyInstaller --noconfirm --clean --onefile --windowed --noupx ^
  --name MultiLevelImageExtractor ^
  --distpath "%DIST%" ^
  --workpath "%BUILD%" ^
  --specpath "%~dp0" ^
  "%~dp0bmp_extractor.py" >>"%LOG%" 2>&1
if errorlevel 1 goto :build_failed
if not exist "%DIST%\MultiLevelImageExtractor.exe" goto :build_failed

echo.
echo Build succeeded:
echo "%DIST%\MultiLevelImageExtractor.exe"
echo.
pause
exit /b 0

:no_python
>>"%LOG%" echo ERROR: Python 3 is unavailable.
echo Python 3 was not found or could not start.
goto :failed

:install_failed
>>"%LOG%" echo ERROR: Could not install PyInstaller.
echo Could not install PyInstaller.
goto :failed

:build_failed
>>"%LOG%" echo ERROR: PyInstaller build failed.
echo PyInstaller build failed.
goto :failed

:failed
echo See the complete log:
echo "%LOG%"
pause
exit /b 1
