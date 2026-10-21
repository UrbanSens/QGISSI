@echo off
rem Run Python with the QGIS (OSGeo4W) interpreter and this repository on the path.
rem Usage: scripts\qgis-python.bat -m qgissi.bench run ...
setlocal
if "%OSGEO4W_ROOT%"=="" set OSGEO4W_ROOT=C:\OSGeo4W
call "%OSGEO4W_ROOT%\bin\python-qgis.bat" %* 2>nul && goto :eof
set PYTHONPATH=%~dp0..;%PYTHONPATH%
set QT_QPA_PLATFORM=offscreen
"%OSGEO4W_ROOT%\bin\python-qgis.bat" %*
