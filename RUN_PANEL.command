#!/bin/bash
cd "$(dirname "$0")"
echo "İsbir Kontrol Paneli için gerekli kütüphaneler kontrol ediliyor..."
pip install -r requirements.txt
echo "Panel başlatılıyor..."
streamlit run app.py
