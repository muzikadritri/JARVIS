@echo off
rem ============================================================================
rem  JARVIS - Schleimy Edition  |  one file that does everything on Windows
rem
rem  Double-click it. It works in two places:
rem    * inside the JARVIS folder   : update, install what is missing, start
rem    * on its own (just this file) : download JARVIS next to it first, then
rem                                    hand over to the copy inside the folder
rem
rem  What it takes care of:
rem    1. JARVIS itself      git clone, or the ZIP from GitHub when git is absent
rem    2. Python 3.11-3.13   64-bit; installed through winget when missing
rem    3. Packages           in a private .venv, re-run only when
rem                          requirements.txt changed
rem    4. Node.js (optional) for the JavaScript skills and Figma code checks
rem    5. Desktop shortcut   "JARVIS" pointing back at this file
rem    6. Start              without a console window (use /console to see logs)
rem
rem  Options:
rem    /install     install and update only, do not start
rem    /reinstall   delete .venv and install everything again
rem    /noupdate    skip "git pull"
rem    /nonode      do not offer to install Node.js
rem    /noshortcut  do not create the desktop shortcut
rem    /console     start in this window and show JARVIS's log output
rem    /figma       open the Figma plugin folder and show the setup steps
rem    /help        this text
rem
rem  Log: jarvis_setup.log in the JARVIS folder.
rem ============================================================================
setlocal EnableExtensions EnableDelayedExpansion
title JARVIS - Schleimy Edition

set "REPO_URL=https://github.com/muzikadritri/JARVIS.git"
set "ZIP_URL=https://github.com/muzikadritri/JARVIS/archive/refs/heads/main.zip"
set "ALL_ARGS=%*"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "LOG="

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

set "OPT_START=1"
set "OPT_REINSTALL=0"
set "OPT_UPDATE=1"
set "OPT_NODE=1"
set "OPT_SHORTCUT=1"
set "OPT_CONSOLE=0"
set "OPT_FIGMA=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="/install"    set "OPT_START=0"
if /i "%~1"=="/reinstall"  set "OPT_REINSTALL=1"
if /i "%~1"=="/noupdate"   set "OPT_UPDATE=0"
if /i "%~1"=="/nonode"     set "OPT_NODE=0"
if /i "%~1"=="/noshortcut" set "OPT_SHORTCUT=0"
if /i "%~1"=="/console"    set "OPT_CONSOLE=1"
if /i "%~1"=="/figma"      set "OPT_FIGMA=1"
if /i "%~1"=="/help"       goto show_help
if /i "%~1"=="/?"          goto show_help
shift
goto parse_args
:args_done

echo.
echo   =============================================
echo      J.A.R.V.I.S.  -  Schleimy Edition
echo      by Schleimy ^& Lukas Chiller
echo   =============================================
echo.

rem ---------------------------------------------------------------------------
rem  1. Where is JARVIS? Inside its folder, or does it have to be downloaded?
rem ---------------------------------------------------------------------------
if exist "%HERE%\main.py" if exist "%HERE%\requirements.txt" (
    set "ROOT=%HERE%"
    goto have_root
)

set "TARGET=%HERE%\JARVIS"
if not exist "%TARGET%\main.py" (
    call :download "%TARGET%"
    if errorlevel 1 goto fail
)
if not exist "%TARGET%\JARVIS.bat" (
    echo [JARVIS] The download is incomplete: "%TARGET%\JARVIS.bat" is missing.
    goto fail
)
echo [JARVIS] Continuing with the copy in "%TARGET%"
endlocal & call "%~dp0JARVIS\JARVIS.bat" %*
exit /b %errorlevel%

:have_root
set "LOG=%ROOT%\jarvis_setup.log"
set "VENV=%ROOT%\.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"
set "STAMP=%VENV%\jarvis_requirements.txt"
call :say "Folder: %ROOT%"

if "%OPT_FIGMA%"=="1" (
    call :figma_help
    start "" explorer "%ROOT%\figma_plugin"
    goto done
)

rem ---------------------------------------------------------------------------
rem  2. Update (git checkouts only)
rem ---------------------------------------------------------------------------
if "%OPT_UPDATE%"=="1" if exist "%ROOT%\.git" (
    where git >nul 2>&1
    if not errorlevel 1 (
        call :say "Checking for updates..."
        git -C "%ROOT%" pull --ff-only
        if errorlevel 1 call :say "Update skipped - offline, or local changes in the folder."
    )
)

rem ---------------------------------------------------------------------------
rem  3. Python 3.11 - 3.13, 64-bit
rem ---------------------------------------------------------------------------
if "%OPT_REINSTALL%"=="1" if exist "%VENV%" (
    call :say "Removing the old environment (/reinstall)..."
    rmdir /s /q "%VENV%"
)

if exist "%VPY%" goto have_venv
call :find_python
if not defined PY call :install_python
if not defined PY (
    call :say "No usable Python was found. Install Python 3.12 (64-bit) from python.org,"
    call :say "tick 'Add python.exe to PATH', then run JARVIS.bat again."
    start "" "https://www.python.org/downloads/windows/"
    goto fail
)
echo [JARVIS] Python: %PY%
call :say "Creating the private environment .venv ..."
%PY% -m venv "%VENV%"
if errorlevel 1 (
    call :say "Creating .venv failed."
    goto fail
)
:have_venv

rem ---------------------------------------------------------------------------
rem  4. Packages - only when requirements.txt changed since the last install
rem ---------------------------------------------------------------------------
set "NEED_INSTALL=1"
if exist "%STAMP%" (
    fc /b "%ROOT%\requirements.txt" "%STAMP%" >nul 2>&1
    if not errorlevel 1 set "NEED_INSTALL=0"
)
if "%NEED_INSTALL%"=="1" (
    call :say "Installing packages - the first time takes 5 to 15 minutes..."
    "%VPY%" -m pip install --disable-pip-version-check --upgrade pip
    pushd "%ROOT%"
    "%VPY%" setup.py
    set "SETUP_RC=!errorlevel!"
    popd
    if not "!SETUP_RC!"=="0" (
        call :say "Package installation failed with code !SETUP_RC! - see the output above."
        call :say "Check the internet connection and run: JARVIS.bat /reinstall"
        goto fail
    )
    copy /y "%ROOT%\requirements.txt" "%STAMP%" >nul
    call :say "Packages installed."
) else (
    call :say "Packages are up to date."
)

rem ---------------------------------------------------------------------------
rem  5. Node.js - optional, for js_studio and the Figma syntax checks
rem ---------------------------------------------------------------------------
if "%OPT_NODE%"=="1" call :ensure_node

rem ---------------------------------------------------------------------------
rem  6. Desktop shortcut
rem ---------------------------------------------------------------------------
if "%OPT_SHORTCUT%"=="1" call :make_shortcut

rem ---------------------------------------------------------------------------
rem  7. Start
rem ---------------------------------------------------------------------------
if "%OPT_START%"=="0" (
    call :say "Installation finished. Start JARVIS with JARVIS.bat or the desktop shortcut."
    call :figma_help
    goto done_pause
)

"%VPY%" -c "import PyQt6.QtWidgets, sounddevice, numpy, google.genai" >nul 2>&1
if errorlevel 1 (
    call :say "Some packages cannot be imported. Repair with: JARVIS.bat /reinstall"
    "%VPY%" -c "import PyQt6.QtWidgets, sounddevice, numpy, google.genai"
    goto fail
)

if not exist "%ROOT%\config\api_keys.json" (
    call :say "First start: JARVIS asks for a free Gemini API key."
    call :say "Get one at https://aistudio.google.com/app/apikey and paste it into the window."
)

if "%OPT_CONSOLE%"=="1" (
    call :say "Starting JARVIS in this window. Closing the window stops JARVIS."
    pushd "%ROOT%"
    "%VPY%" main.py
    set "RUN_RC=!errorlevel!"
    popd
    if not "!RUN_RC!"=="0" (
        call :say "JARVIS exited with code !RUN_RC!."
        goto fail
    )
    goto done
)

call :say "Starting JARVIS..."
start "JARVIS" /D "%ROOT%" "%VPYW%" main.py
call :say "JARVIS is running. Figma live design: JARVIS.bat /figma"
timeout /t 4 >nul
goto done


rem ============================================================================
rem  Subroutines
rem ============================================================================
:say
echo [JARVIS] %~1
if defined LOG >>"%LOG%" echo [%date% %time%] %~1
exit /b 0

:download
set "DEST=%~1"
where git >nul 2>&1
if not errorlevel 1 (
    echo [JARVIS] Downloading JARVIS with git into "%DEST%" ...
    git clone --depth 1 "%REPO_URL%" "%DEST%"
    if not errorlevel 1 exit /b 0
    echo [JARVIS] git clone failed - trying the ZIP download instead.
)
echo [JARVIS] Downloading JARVIS as ZIP into "%DEST%" ...
set "ZIP_FILE=%TEMP%\jarvis_main_%RANDOM%.zip"
set "UNZIP_DIR=%TEMP%\jarvis_unzip_%RANDOM%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri $env:ZIP_URL -OutFile $env:ZIP_FILE; Expand-Archive -Force -Path $env:ZIP_FILE -DestinationPath $env:UNZIP_DIR"
if errorlevel 1 (
    echo [JARVIS] Download failed. Check the internet connection, or download
    echo [JARVIS] the ZIP by hand: %ZIP_URL%
    exit /b 1
)
robocopy "%UNZIP_DIR%\JARVIS-main" "%DEST%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo [JARVIS] Copying the downloaded files failed.
    exit /b 1
)
del /q "%ZIP_FILE%" >nul 2>&1
rmdir /s /q "%UNZIP_DIR%" >nul 2>&1
echo [JARVIS] Downloaded. Updates later: run JARVIS.bat again - with git installed
echo [JARVIS] it updates itself, otherwise download the ZIP again.
exit /b 0

:find_python
set "PY="
set "PY_TEST=import sys, struct; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) and struct.calcsize('P') == 8 else 1)"
for %%V in (3.12 3.13 3.11) do (
    if not defined PY (
        py -%%V -c "!PY_TEST!" >nul 2>&1
        if not errorlevel 1 set "PY=py -%%V"
    )
)
if defined PY exit /b 0
python -c "!PY_TEST!" >nul 2>&1
if not errorlevel 1 (
    set "PY=python"
    exit /b 0
)
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python311\python.exe"
) do (
    if not defined PY if exist "%%~P" (
        "%%~P" -c "!PY_TEST!" >nul 2>&1
        if not errorlevel 1 set "PY="%%~P""
    )
)
exit /b 0

:install_python
where winget >nul 2>&1
if errorlevel 1 exit /b 0
call :say "Python 3.12 is missing - installing it with winget (takes a minute)..."
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :find_python
exit /b 0

:ensure_node
where node >nul 2>&1
if not errorlevel 1 exit /b 0
if exist "%ProgramFiles%\nodejs\node.exe" exit /b 0
where winget >nul 2>&1
if errorlevel 1 (
    call :say "Node.js is missing - optional, for the JavaScript and Figma skills: https://nodejs.org"
    exit /b 0
)
choice /c YN /t 20 /d Y /m "[JARVIS] Install Node.js (optional, for JavaScript and Figma skills)"
if errorlevel 2 (
    call :say "Node.js skipped. JavaScript skills stay limited until it is installed."
    exit /b 0
)
winget install -e --id OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    call :say "Node.js could not be installed automatically: https://nodejs.org"
) else (
    call :say "Node.js installed."
)
exit /b 0

:make_shortcut
set "SC_TARGET=%ROOT%\JARVIS.bat"
set "SC_ICON=%ROOT%\config\jarvis.ico"
set "SC_DIR=%ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Join-Path ([Environment]::GetFolderPath('Desktop')) 'JARVIS.lnk'; if (-not (Test-Path -LiteralPath $p)) { $s = (New-Object -ComObject WScript.Shell).CreateShortcut($p); $s.TargetPath = $env:SC_TARGET; $s.WorkingDirectory = $env:SC_DIR; if (Test-Path -LiteralPath $env:SC_ICON) { $s.IconLocation = $env:SC_ICON }; $s.WindowStyle = 1; $s.Description = 'JARVIS - Schleimy Edition'; $s.Save(); Write-Output 'created' }" > "%TEMP%\jarvis_sc.txt" 2>nul
findstr /c:"created" "%TEMP%\jarvis_sc.txt" >nul 2>&1
if not errorlevel 1 call :say "Desktop shortcut 'JARVIS' created."
del /q "%TEMP%\jarvis_sc.txt" >nul 2>&1
exit /b 0

:figma_help
echo.
echo   FIGMA LIVE DESIGN - one-time setup
echo   1. Open the Figma DESKTOP app and any design file.
echo   2. Menu: Plugins - Development - Import plugin from manifest...
echo   3. Choose: %ROOT%\figma_plugin\manifest.json
echo   4. Run: Plugins - Development - JARVIS Live Designer, keep its window open.
echo   Then tell JARVIS: "Design a landing page for ..."
echo.
exit /b 0

:show_help
echo.
echo   JARVIS.bat              update, install what is missing, start
echo   JARVIS.bat /install     install and update only
echo   JARVIS.bat /reinstall   reinstall all packages from scratch
echo   JARVIS.bat /console     start with visible log output
echo   JARVIS.bat /figma       Figma plugin setup
echo   JARVIS.bat /noupdate /nonode /noshortcut   skip those steps
echo.
exit /b 0

:fail
echo.
echo [JARVIS] Something went wrong - details above
if defined LOG echo [JARVIS] and in "%LOG%".
if defined LOG >>"%LOG%" echo [%date% %time%] FAILED
pause
exit /b 1

:done_pause
pause
:done
exit /b 0
