import pandas as pd
import openpyxl

wb = openpyxl.load_workbook("custom_output/cizelge_sonuclari.xlsx")
print("Sheets in Excel:", wb.sheetnames)
