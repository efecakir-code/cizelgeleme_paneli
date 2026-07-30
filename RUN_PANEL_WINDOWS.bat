@echo off
title ISBIR Planlama Paneli (Windows)
color 0A
echo.
echo ===================================================
echo ISBIR Optimizasyon ve Cizelgeleme Paneli Baslatiliyor
echo ===================================================
echo.

echo 1. Gereksinimler kontrol ediliyor...
python -m pip install -r requirements.txt

echo.
echo 2. Panel Aciliyor...
echo Lutfen acilan tarayici penceresini kapatmayin. 
echo Bu siyah ekran acik kaldikca panel calismaya devam eder.
echo Paneli kapatmak icin bu siyah ekrani kapatabilirsiniz.
echo.

streamlit run app.py

pause
