@echo off
setlocal
pushd "%~dp0"
py -3 -B -X utf8 viewer.py %*
set "KGF_VIEWER_EXIT=%ERRORLEVEL%"
popd
if not "%KGF_VIEWER_EXIT%"=="0" pause
exit /b %KGF_VIEWER_EXIT%
