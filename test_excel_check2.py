import pandas as pd
excel_path = "custom_output/cizelge_sonuclari.xlsx"
wb = pd.ExcelFile(excel_path)
print("Sayfalar:", wb.sheet_names)
