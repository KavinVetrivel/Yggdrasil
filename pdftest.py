import pdfplumber

with pdfplumber.open("REGULATIONS.pdf") as pdf:
    for i, page in enumerate(pdf.pages):
        print(f"\n--- PAGE {i+1} ---")
        tables = page.extract_tables()
        print(f"Tables found: {len(tables)}")
        for t in tables:
            print(t)
        print(page.extract_text())