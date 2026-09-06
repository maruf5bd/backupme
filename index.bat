@echo off
title BackupMe Command Index
chcp 65001 >nul 2>nul
setlocal enabledelayedexpansion

cd /d "%~dp0"
set "PATH=%PATH%;%APPDATA%\Python\Python313\Scripts"

echo.
echo  =========================================================
echo         BackupMe -- Command Index
echo  =========================================================
echo.

:menu
echo  --- Backup -----------------------------------------------
echo   1   backup          Backup files/directories
echo   2   upload-bundle   Batch-upload small files as tar
echo   3   convert         Convert backend files to dedup
echo  ----------------------------------------------------------
echo.
echo  --- Restore ----------------------------------------------
echo   4   download        Download a file by ID or path
echo  ----------------------------------------------------------
echo.
echo  --- List / Search ----------------------------------------
echo   5   list            List backed-up files
echo   6   search          Full-text search files
echo   7   stats           Storage statistics
echo   8   hash-lookup     Find file by SHA256 hash
echo   9   backends        List configured backends
echo  ----------------------------------------------------------
echo.
echo  --- Encryption -------------------------------------------
echo  10   encrypt init    Generate encryption key
echo  11   encrypt status  Check encryption key status
echo  ----------------------------------------------------------
echo.
echo  --- Compression ------------------------------------------
echo  12   recompress      Recompress blobs to new algo/level
echo  ----------------------------------------------------------
echo.
echo  --- Database ---------------------------------------------
echo  13   db check        Run integrity check
echo  14   db snapshot     Create DB snapshot
echo  15   db restore      Restore DB from replica
echo  16   db rebuild      Rebuild DB from manifests
echo  17   db info         Show DB metadata
echo  18   db oplog        View operation log
echo  ----------------------------------------------------------
echo.
echo  --- Verify / Report --------------------------------------
echo  19   report list     List integrity reports
echo  20   report get      Show latest report
echo  21   report verify   Run random verify-restore
echo  22   report integrity Full integrity report
echo  ----------------------------------------------------------
echo.
echo  --- Daemon / Web -----------------------------------------
echo  23   web             Start web dashboard
echo  24   daemon          Start/stop daemon scheduler
echo  ----------------------------------------------------------
echo.
echo  --- Backend Selection ------------------------------------
echo  25   select-backend  Manually select a backend
echo  ----------------------------------------------------------
echo.
echo  =========================================================
echo.
set /p choice="Enter number (or Q to quit, H for help): "

if "%choice%"=="Q" goto :eof
if "%choice%"=="q" goto :eof
if "%choice%"=="H" goto :help
if "%choice%"=="h" goto :help

if "%choice%"=="1" goto :backup
if "%choice%"=="2" goto :bundle
if "%choice%"=="3" goto :convert
if "%choice%"=="4" goto :download
if "%choice%"=="5" goto :list
if "%choice%"=="6" goto :search
if "%choice%"=="7" goto :stats
if "%choice%"=="8" goto :hash
if "%choice%"=="9" goto :backends
if "%choice%"=="10" goto :encrypt_init
if "%choice%"=="11" goto :encrypt_status
if "%choice%"=="12" goto :recompress
if "%choice%"=="13" goto :db_check
if "%choice%"=="14" goto :db_snapshot
if "%choice%"=="15" goto :db_restore
if "%choice%"=="16" goto :db_rebuild
if "%choice%"=="17" goto :db_info
if "%choice%"=="18" goto :db_oplog
if "%choice%"=="19" goto :report_list
if "%choice%"=="20" goto :report_get
if "%choice%"=="21" goto :report_verify
if "%choice%"=="22" goto :report_integrity
if "%choice%"=="23" goto :web
if "%choice%"=="24" goto :daemon
if "%choice%"=="25" goto :select_backend

echo Invalid choice.
timeout /t 2 >nul
cls
goto :menu

rem -------- HELP --------
:help
echo.
echo  Example: backupme backup C:\Users\Maruf\Documents --recursive
echo.
echo  All commands support --help for detailed options:
echo    backupme backup --help
echo    backupme download --help
echo    backupme db --help
echo.
pause
cls
goto :menu

rem -------- BACKUP --------
:backup
echo.
echo  USAGE:  backupme backup [OPTIONS] SOURCES...
echo.
echo  Examples:
echo    backupme backup file.txt
echo    backupme backup C:\Users\Maruf\Documents --recursive --backend local_disk
echo    backupme backup C:\Users\Maruf\Docs --recursive --redundancy 2
echo    backupme backup C:\Users\Maruf\Docs --recursive --no-skip
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme backup %args%
)
pause
cls
goto :menu

rem -------- UPLOAD-BUNDLE --------
:bundle
echo.
echo  USAGE:  backupme upload-bundle [OPTIONS] DIRECTORY
echo.
echo  Batch-uploads 1KB-64KB files as a compressed tar bundle.
echo.
echo  Example:
echo    backupme upload-bundle C:\Users\Maruf\Documents\configs -b local_disk
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme upload-bundle %args%
)
pause
cls
goto :menu

rem -------- CONVERT --------
:convert
echo.
echo  USAGE:  backupme convert [OPTIONS] BACKEND_NAME
echo.
echo  Options:  --prefix PATH   --delete   --target BACKEND   --resume
echo.
echo  Examples:
echo    backupme convert usb_drive
echo    backupme convert usb_drive --delete --target local_disk
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme convert %args%
)
pause
cls
goto :menu

rem -------- DOWNLOAD --------
:download
echo.
echo  USAGE:  backupme download [OPTIONS] FILE_ID [DEST_PATH]
echo.
echo  Examples:
echo    backupme download 5
echo    backupme download 5 C:\Users\Maruf\restored\file.txt
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme download %args%
)
pause
cls
goto :menu

rem -------- LIST --------
:list
echo.
echo  USAGE:  backupme list [OPTIONS]
echo.
echo  Options:  --source ROOT   --backend NAME   --hash VALUE   --limit N   --offset N
echo.
echo  Examples:
echo    backupme list
echo    backupme list --limit 20
echo    backupme list --source C:\Users\Maruf\Documents
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme list %args%
)
pause
cls
goto :menu

rem -------- SEARCH --------
:search
echo.
echo  USAGE:  backupme search [OPTIONS] QUERY
echo.
echo  Options:  --content   --source ROOT
echo.
echo  Examples:
echo    backupme search "report"
echo    backupme search --content "meeting notes"
echo    backupme search --source "photos" "beach"
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme search %args%
)
pause
cls
goto :menu

rem -------- STATS --------
:stats
echo.
echo  USAGE:  backupme stats
echo.
backupme stats
pause
cls
goto :menu

rem -------- HASH-LOOKUP --------
:hash
echo.
echo  USAGE:  backupme hash-lookup SHA256_HASH
echo.
echo  Example:
echo    backupme hash-lookup a1b2c3d4...
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme hash-lookup %args%
)
pause
cls
goto :menu

rem -------- BACKENDS --------
:backends
echo.
echo  USAGE:  backupme backends
echo.
backupme backends
pause
cls
goto :menu

rem -------- ENCRYPT INIT --------
:encrypt_init
echo.
echo  USAGE:  backupme encrypt init
echo.
echo  Creates a new AES-256-GCM encryption key.
backupme encrypt init
pause
cls
goto :menu

rem -------- ENCRYPT STATUS --------
:encrypt_status
echo.
echo  USAGE:  backupme encrypt status
echo.
backupme encrypt status
pause
cls
goto :menu

rem -------- RECOMPRESS --------
:recompress
echo.
echo  USAGE:  backupme recompress [OPTIONS]
echo.
echo  Options:  --algo zstd   --level 3   --backend NAME
echo.
echo  Examples:
echo    backupme recompress
echo    backupme recompress --algo zstd --level 1 --backend usb_drive
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme recompress %args%
)
pause
cls
goto :menu

rem -------- DB CHECK --------
:db_check
echo.
echo  USAGE:  backupme db check [OPTIONS]
echo.
echo  Examples:
echo    backupme db check
echo    backupme db check --full
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme db check %args%
)
pause
cls
goto :menu

rem -------- DB SNAPSHOT --------
:db_snapshot
echo.
echo  USAGE:  backupme db snapshot
echo.
backupme db snapshot
pause
cls
goto :menu

rem -------- DB RESTORE --------
:db_restore
echo.
echo  USAGE:  backupme db restore [OPTIONS]
echo.
echo  Options:  --from BACKEND   --snapshot FILE
echo.
echo  Example:
echo    backupme db restore --from usb_drive
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme db restore %args%
)
pause
cls
goto :menu

rem -------- DB REBUILD --------
:db_rebuild
echo.
echo  USAGE:  backupme db rebuild
echo.
echo  Rebuilds the database by scanning all backend manifests.
echo.
set /p confirm="This will rebuild from scratch. Continue? (y/N): "
if /i "%confirm%"=="y" (
    backupme db rebuild
)
pause
cls
goto :menu

rem -------- DB INFO --------
:db_info
echo.
echo  USAGE:  backupme db info
echo.
backupme db info
pause
cls
goto :menu

rem -------- DB OPLOG --------
:db_oplog
echo.
echo  USAGE:  backupme db oplog [OPTIONS]
echo.
echo  Options:  --pending (show only pending operations)
echo.
backupme db oplog
pause
cls
goto :menu

rem -------- REPORT LIST --------
:report_list
echo.
echo  USAGE:  backupme report list
echo.
backupme report list
pause
cls
goto :menu

rem -------- REPORT GET --------
:report_get
echo.
echo  USAGE:  backupme report get [OPTIONS]
echo.
echo  Options:  --format html|json|text
echo.
echo  Example:
echo    backupme report get --format json
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme report get %args%
)
pause
cls
goto :menu

rem -------- REPORT VERIFY --------
:report_verify
echo.
echo  USAGE:  backupme report verify [OPTIONS]
echo.
echo  Options:  --count N (default 10)   --backend NAME
echo.
echo  Example:
echo    backupme report verify --count 5
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme report verify %args%
)
pause
cls
goto :menu

rem -------- REPORT INTEGRITY --------
:report_integrity
echo.
echo  USAGE:  backupme report integrity
echo.
backupme report integrity
pause
cls
goto :menu

rem -------- WEB --------
:web
echo.
echo  USAGE:  backupme web [OPTIONS]
echo.
echo  Options:  --host 127.0.0.1  --port 8080
echo.
echo  Examples:
echo    backupme web
echo    backupme web --port 8080
echo.
set /p args="Enter args: "
if "%args%"=="" (
    echo Starting web dashboard on default port...
    start http://127.0.0.1:8080
    backupme web
) else (
    echo Starting web dashboard...
    start http://127.0.0.1:8080
    backupme web %args%
)
pause
cls
goto :menu

rem -------- DAEMON --------
:daemon
echo.
echo  USAGE:  backupme daemon start|stop|status
echo.
echo  Examples:
echo    backupme daemon start
echo    backupme daemon status
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme daemon %args%
)
pause
cls
goto :menu

rem -------- SELECT BACKEND --------
:select_backend
echo.
echo  USAGE:  backupme select-backend QUERY
echo.
echo  Fuzzy-matches a backend name, shows score.
echo.
echo  Example:
echo    backupme select-backend usb
echo.
set /p args="Enter args: "
if not "%args%"=="" (
    backupme select-backend %args%
)
pause
cls
goto :menu
