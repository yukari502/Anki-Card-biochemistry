@echo off
cd /d "%~dp0"
python markdown_to_anki.py
if errorlevel 1 pause
