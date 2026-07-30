import openpyxl

excel_path = "custom_output/cizelge_sonuclari.xlsx"
wb = openpyxl.load_workbook(excel_path)
ws = wb["Opt. Gantt Görseli"]

colored_cells = 0
for row in ws.iter_rows():
    for cell in row:
        if cell.fill and cell.fill.start_color and cell.fill.start_color.index != '00000000':
            colored_cells += 1

print(f"Toplam renkli hücre sayısı: {colored_cells}")
