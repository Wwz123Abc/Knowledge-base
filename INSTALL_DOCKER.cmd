@echo off
title Enterprise RAG - Docker Installer
powershell.exe -NoExit -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-docker-local.ps1"
