@echo off
:: fix-permissions.bat — Windows equivalent of `make fix-permissions`
::
:: Runs a one-shot Alpine container that chowns every bind-mount directory to
:: HERMES_UID:HERMES_GID, resolving errno 13 (Permission Denied) caused by
:: Docker Desktop mapping host files to root inside containers.
::
:: Requirements: Docker Desktop running, .env present (or defaults are used).

setlocal EnableDelayedExpansion

:: ── Load .env if present ──────────────────────────────────────────────────
:: Read key=value lines, skip comments and blanks.
set "HERMES_UID=1000"
set "HERMES_GID=1000"
set "DATA_DIR="
set "SHARED_DIR="
set "WM_DATA_DIR="
set "WM_LSP_CACHE_DIR="
set "CADDY_DATA_DIR="
set "CADDY_CONFIG_DIR="

if exist ".env" (
    for /f "usebackq tokens=1* delims==" %%A in (`findstr /v "^#" .env ^| findstr /v "^$"`) do (
        set "%%A=%%B"
    )
)

:: ── Convert USERPROFILE to a Docker-compatible POSIX path ─────────────────
:: C:\Users\foo  →  /c/Users/foo
set "USERPROFILE_POSIX=%USERPROFILE%"
set "USERPROFILE_POSIX=%USERPROFILE_POSIX:\=/%"
:: Replace drive letter prefix  C:/  →  /c/
for /f "tokens=1,2 delims=:/" %%D in ("%USERPROFILE_POSIX%") do (
    set "DRIVE_LOWER=%%D"
    :: tolower the drive letter via a simple A-Z lookup
    for %%L in (a b c d e f g h i j k l m n o p q r s t u v w x y z) do (
        if /i "%%D"=="%%L" set "DRIVE_LOWER=%%L"
    )
    set "USERPROFILE_POSIX=/!DRIVE_LOWER!/%%E"
)
:: Re-attach the rest of the path (everything after drive:/)
:: We already substituted \ → / above, so just fix the prefix
set "TMP_PATH=%USERPROFILE:\=/%"
for /f "tokens=1* delims=/" %%X in ("%TMP_PATH%") do (
    set "REST=%%Y"
)
set "DOCKER_HOME=/!DRIVE_LOWER!/%REST%"
:: Remove accidental double slashes
set "DOCKER_HOME=%DOCKER_HOME://=/%"

:: ── Resolve directory paths (use .env values or defaults) ─────────────────
if "%DATA_DIR%"=="" set "DATA_DIR=%DOCKER_HOME%/.hermes"
if "%SHARED_DIR%"=="" set "SHARED_DIR=%DOCKER_HOME%/.shared_agent_data"
if "%WM_DATA_DIR%"=="" set "WM_DATA_DIR=%DOCKER_HOME%/.windmill"
if "%WM_LSP_CACHE_DIR%"=="" set "WM_LSP_CACHE_DIR=%DOCKER_HOME%/.windmill/lsp_cache"
if "%CADDY_DATA_DIR%"=="" set "CADDY_DATA_DIR=%DOCKER_HOME%/.caddy/data"
if "%CADDY_CONFIG_DIR%"=="" set "CADDY_CONFIG_DIR=%DOCKER_HOME%/.caddy/config"

:: ── Normalise any remaining backslashes that came from .env ───────────────
set "DATA_DIR=%DATA_DIR:\=/%"
set "SHARED_DIR=%SHARED_DIR:\=/%"
set "WM_DATA_DIR=%WM_DATA_DIR:\=/%"
set "WM_LSP_CACHE_DIR=%WM_LSP_CACHE_DIR:\=/%"
set "CADDY_DATA_DIR=%CADDY_DATA_DIR:\=/%"
set "CADDY_CONFIG_DIR=%CADDY_CONFIG_DIR:\=/%"

:: ── Verify Docker is available ────────────────────────────────────────────
docker version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] docker not found or Docker Desktop is not running.
    exit /b 1
)

:: ── Create directories if they don't exist ────────────────────────────────
:: Docker Desktop can't mount a path that doesn't exist yet.
for %%D in (
    "%DATA_DIR%"
    "%SHARED_DIR%"
    "%WM_DATA_DIR%"
    "%WM_DATA_DIR%/db"
    "%WM_DATA_DIR%/logs"
    "%WM_DATA_DIR%/cache"
    "%WM_LSP_CACHE_DIR%"
    "%CADDY_DATA_DIR%"
    "%CADDY_CONFIG_DIR%"
) do (
    :: Convert back to Windows path for mkdir
    set "WIN_PATH=%%~D"
    set "WIN_PATH=!WIN_PATH:/=\!"
    if not exist "!WIN_PATH!" mkdir "!WIN_PATH!" 2>nul
)

echo Fixing ownership to %HERMES_UID%:%HERMES_GID% on bind-mount directories...
echo   DATA_DIR       : %DATA_DIR%
echo   SHARED_DIR     : %SHARED_DIR%
echo   WM_DATA_DIR    : %WM_DATA_DIR%
echo   WM_LSP_CACHE   : %WM_LSP_CACHE_DIR%
echo   CADDY_DATA_DIR : %CADDY_DATA_DIR%
echo   CADDY_CONFIG   : %CADDY_CONFIG_DIR%
echo.

docker run --rm ^
    -v "%DATA_DIR%:/mnt/data" ^
    -v "%SHARED_DIR%:/mnt/shared" ^
    -v "%WM_DATA_DIR%:/mnt/wm" ^
    -v "%WM_LSP_CACHE_DIR%:/mnt/wm_lsp" ^
    -v "%CADDY_DATA_DIR%:/mnt/caddy_data" ^
    -v "%CADDY_CONFIG_DIR%:/mnt/caddy_config" ^
    alpine:3 ^
    sh -c "chown -R %HERMES_UID%:%HERMES_GID% /mnt/data /mnt/shared /mnt/wm /mnt/wm_lsp /mnt/caddy_data /mnt/caddy_config && chmod -R u+rwX /mnt/data /mnt/shared /mnt/wm /mnt/wm_lsp /mnt/caddy_data /mnt/caddy_config"

if errorlevel 1 (
    echo [ERROR] Docker container exited with an error — check the output above.
    exit /b 1
)

echo.
echo [OK] Ownership corrected. You can now run: docker compose up -d
endlocal
