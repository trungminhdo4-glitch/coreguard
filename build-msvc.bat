@echo off
setlocal
set "VCVARS=%COREGUARD_VCVARS%"
if "%VCVARS%"=="" (
    for /f "delims=" %%P in ('where vcvars64.bat 2^>nul') do if not defined VCVARS set "VCVARS=%%P"
)
if "%VCVARS%"=="" (
    echo Could not locate vcvars64.bat. Set COREGUARD_VCVARS to the MSVC environment script.
    exit /b 1
)
call "%VCVARS%"
if errorlevel 1 exit /b %errorlevel%
if not exist build mkdir build
rem /wd28301 is limited to the current SDK's unannotated-header diagnostic.
cl /nologo /W4 /WX /analyze /wd28301 /std:c11 /TC /Iinclude /Isrc /c /Fo:build\process.obj src\process.c
if errorlevel 1 exit /b %errorlevel%
cl /nologo /W4 /WX /analyze /wd28301 /std:c11 /TC /Iinclude /Isrc /c /Fo:build\windows_process.obj src\platform\windows_process.c
if errorlevel 1 exit /b %errorlevel%
lib /nologo /out:build\coreguard.lib build\process.obj build\windows_process.obj
if errorlevel 1 exit /b %errorlevel%
cl /nologo /W4 /WX /analyze /wd28301 /std:c11 /TC /Iinclude /Isrc /c /Fo:build\main.obj src\main.c
if errorlevel 1 exit /b %errorlevel%
cl /nologo /Fe:build\coreguard.exe /Fd:build\coreguard.pdb build\main.obj build\coreguard.lib
exit /b %errorlevel%
