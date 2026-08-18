#!/usr/bin/env python
"""Generates the two multimodal documentation fixtures (master doc §22.1):

  - erd_sales_customers.png   — an ERD image "documenting" Sales.Customers
  - data_dictionary_co_customers.pdf — a PDF data dictionary "documenting" CO.CUSTOMERS

Both are self-authored and DELIBERATELY drift from the real introspected
schema — see README.md in this directory for the exact, itemized list of
seeded discrepancies. This is the same honesty discipline as
DATA_SOURCES.md and simulator/failure_injector/: every injected
discrepancy is catalogued, never a silent bug.

Usage (from repo root):
    python simulator/documentation/generate_fixtures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent

# Documented schema for Sales.Customers (SQL Server / WWI) — an ERD image.
# See README.md for the itemized drift vs the real introspected table.
ERD_COLUMNS = [
    ("CustomerID", "INT", "PK"),
    ("CustomerName", "NVARCHAR(100)", ""),
    ("EmailAddress", "NVARCHAR(100)", ""),          # drift: does not exist in the real table
    ("PhoneNumber", "CHAR(10)", "PUBLIC"),           # drift: real type is NVARCHAR; real classification is PII
    ("CreditLimit", "FLOAT", ""),                     # drift: real type is DECIMAL
]

# Documented schema for CO.CUSTOMERS (Oracle corpus) — a PDF data dictionary.
# See README.md for the itemized drift vs the real corpus DDL.
DICT_COLUMNS = [
    ("CUSTOMER_ID", "NUMBER(10)", "Primary key", "METADATA"),
    ("CUSTOMER_NAME", "VARCHAR2(100)", "Legal customer name", "METADATA"),
    ("EMAIL_ADDRESS", "VARCHAR2(150)", "Contact email", "PUBLIC"),   # drift: real classification is PII
    ("ACCOUNT_MGR_ID", "VARCHAR2(20)", "Assigned account manager", "METADATA"),  # drift: real type is NUMBER
    # drift: NATIONAL_ID exists in the real table but is entirely absent from this dictionary — a shadow PII asset.
]


def generate_erd_image() -> Path:
    width, height = 420, 60 + 28 * (len(ERD_COLUMNS) + 1)
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    try:
        font_title = ImageFont.truetype("arial.ttf", 16)
        font_row = ImageFont.truetype("arial.ttf", 13)
    except OSError:
        font_title = ImageFont.load_default()
        font_row = ImageFont.load_default()

    draw.rectangle([0, 0, width - 1, height - 1], outline="black", width=2)
    draw.rectangle([0, 0, width - 1, 34], fill="#2c3e50")
    draw.text((10, 8), "Sales.Customers (documented)", fill="white", font=font_title)
    draw.line([0, 34, width, 34], fill="black", width=1)

    y = 44
    for name, dtype, note in ERD_COLUMNS:
        draw.text((10, y), name, fill="black", font=font_row)
        draw.text((190, y), dtype, fill="#333333", font=font_row)
        draw.text((330, y), note, fill="#c0392b", font=font_row)
        y += 28

    out_path = OUT_DIR / "erd_sales_customers.png"
    img.save(out_path)
    return out_path


def generate_data_dictionary_pdf() -> Path:
    out_path = OUT_DIR / "data_dictionary_co_customers.pdf"
    doc = SimpleDocTemplate(str(out_path), pagesize=letter)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("Data Dictionary — CO.CUSTOMERS", styles["Title"]),
        Paragraph("Customer Orders schema (Oracle-dialect corpus)", styles["Normal"]),
        Spacer(1, 16),
    ]

    table_data = [["Column", "Type", "Description", "Sensitivity"]] + [list(row) for row in DICT_COLUMNS]
    table = Table(table_data, colWidths=[110, 110, 180, 90])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f4f4")]),
            ]
        )
    )
    elements.append(table)
    doc.build(elements)
    return out_path


def main() -> None:
    erd_path = generate_erd_image()
    pdf_path = generate_data_dictionary_pdf()
    print(f"Generated: {erd_path}")
    print(f"Generated: {pdf_path}")


if __name__ == "__main__":
    main()
