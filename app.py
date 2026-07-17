import streamlit as st
import pandas as pd
import re
import io
from pathlib import Path
from docx import Document
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm as mm_unit
from pypdf import PdfWriter, PdfReader

INVENTORY_FILE = "inventory_master_final.csv"
RECEIPTS_DIR = Path("receipts")
RECEIPTS_DIR.mkdir(exist_ok=True)

RECEIPT_WIDTH_MM = 80
RECEIPT_WIDTH_PT = RECEIPT_WIDTH_MM * mm_unit
LINE_HEIGHT = 12
FONT_SIZE = 9
MARGIN = 10

st.set_page_config(page_title="Perfume Inventory & Pick Assistant", layout="wide")


def normalize_sku(text):
    return str(text).strip().zfill(3)


def load_inventory():
    df = pd.read_csv(INVENTORY_FILE, dtype={"SKU": str})

    if df.empty:
        return pd.DataFrame(columns=[
            "SKU", "Location", "As Entered", "Standardized Full Name", "Qty",
            "Needs Confirmation", "Stock Type", "Pick Priority", "Status"
        ])

    df["SKU"] = df["SKU"].astype(str).str.zfill(3)
    df["Qty"] = pd.to_numeric(df["Qty"], errors="coerce").fillna(0).astype(int)
    df["Pick Priority"] = pd.to_numeric(df["Pick Priority"], errors="coerce").fillna(999).astype(int)
    return df


def save_inventory(df):
    df = df.copy()
    df["SKU"] = df["SKU"].astype(str).str.zfill(3)
    df["Qty"] = pd.to_numeric(df["Qty"], errors="coerce").fillna(0).astype(int)
    df["Pick Priority"] = pd.to_numeric(df["Pick Priority"], errors="coerce").fillna(999).astype(int)
    df.to_csv(INVENTORY_FILE, index=False)


def get_pick_locations(sku, qty_needed, df):
    sku = normalize_sku(sku)
    candidates = df[df["SKU"].astype(str).str.zfill(3) == sku].copy()
    candidates["Qty"] = pd.to_numeric(candidates["Qty"], errors="coerce").fillna(0).astype(int)
    candidates["Pick Priority"] = pd.to_numeric(candidates["Pick Priority"], errors="coerce").fillna(999).astype(int)
    candidates = candidates[candidates["Qty"] > 0].sort_values(["Pick Priority", "Location"])

    picks = []
    remaining = int(qty_needed)

    for _, row in candidates.iterrows():
        if remaining <= 0:
            break

        take = min(remaining, int(row["Qty"]))
        picks.append({
            "location": row["Location"],
            "take": take,
            "stock_type": row["Stock Type"],
            "sku": sku,
            "name": row["Standardized Full Name"]
        })
        remaining -= take

    if remaining > 0:
        picks.append({
            "location": None,
            "take": remaining,
            "stock_type": "SHORTAGE",
            "sku": sku,
            "name": candidates.iloc[0]["Standardized Full Name"] if not candidates.empty else "Unknown SKU"
        })

    return picks


def extract_docx_lines(docx_bytes):
    doc = Document(io.BytesIO(docx_bytes))
    lines = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            lines.append(text)
    return lines


def build_receipt_pdf_bytes(lines):
    height_pt = MARGIN * 2 + LINE_HEIGHT * (len(lines) + 2)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(RECEIPT_WIDTH_PT, height_pt))
    c.setFont("Helvetica", FONT_SIZE)
    y = height_pt - MARGIN - LINE_HEIGHT

    for line in lines:
        c.drawString(MARGIN, y, line[:60])
        y -= LINE_HEIGHT

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read(), height_pt


def load_receipts_from_directory():
    rows = []
    for ext in ["*.docx", "*.pdf"]:
        for file in RECEIPTS_DIR.glob(ext):
            sku = normalize_sku(file.stem)
            rows.append({
                "sku": sku,
                "filename": file.name,
                "file_type": file.suffix.lower().replace(".", ""),
                "path": str(file)
            })

    if not rows:
        return pd.DataFrame(columns=["sku", "filename", "file_type", "path"])

    return pd.DataFrame(rows).sort_values(["sku", "filename"]).reset_index(drop=True)


def get_receipt_bytes_and_type(sku):
    sku = normalize_sku(sku)

    docx_path = RECEIPTS_DIR / f"{sku}.docx"
    if docx_path.exists():
        return docx_path.read_bytes(), "docx"

    pdf_path = RECEIPTS_DIR / f"{sku}.pdf"
    if pdf_path.exists():
        return pdf_path.read_bytes(), "pdf"

    return None, None


def save_uploaded_receipts(uploaded_files):
    saved = []
    skipped = []

    for uploaded in uploaded_files:
        suffix = uploaded.name.split(".")[-1].lower()
        stem = uploaded.name.rsplit(".", 1)[0].strip()

        if suffix not in ["pdf", "docx"]:
            skipped.append(uploaded.name)
            continue

        if not stem.isdigit():
            skipped.append(uploaded.name)
            continue

        sku = normalize_sku(stem)
        out_path = RECEIPTS_DIR / f"{sku}.{suffix}"
        out_path.write_bytes(uploaded.read())
        saved.append(out_path.name)

    return saved, skipped


def generate_receipt_pdf_for_picks(pick_plan, inventory_df):
    output_writer = PdfWriter()
    missing = []
    included = []

    for item in pick_plan:
        sku = normalize_sku(item["sku"])
        row_match = inventory_df[inventory_df["SKU"].astype(str).str.zfill(3) == sku]
        if row_match.empty:
            continue

        unpackaged_picks = [p for p in item["picks"] if p["stock_type"] == "Unpackaged"]
        copies_needed = sum(int(p["take"]) for p in unpackaged_picks)
        if copies_needed <= 0:
            continue

        receipt_bytes, file_type = get_receipt_bytes_and_type(sku)
        if not receipt_bytes:
            missing.append(sku)
            continue

        if file_type != "docx":
            missing.append(f"{sku} (receipt is not DOCX)")
            continue

        lines = extract_docx_lines(receipt_bytes)
        pdf_bytes, _ = build_receipt_pdf_bytes(lines)
        reader = PdfReader(io.BytesIO(pdf_bytes))

        for _ in range(copies_needed):
            for page in reader.pages:
                output_writer.add_page(page)

        name = row_match.iloc[0]["Standardized Full Name"]
        included.append(f"{sku} - {name} x{copies_needed}")

    if not included:
        return None, missing, included

    buf = io.BytesIO()
    output_writer.write(buf)
    return buf.getvalue(), missing, included


def get_receipt_print_summary(pick_plan, inventory_df):
    summary = []

    for item in pick_plan:
        sku = normalize_sku(item["sku"])
        row_match = inventory_df[inventory_df["SKU"] == sku]
        if row_match.empty:
            continue

        unpackaged_picks = [p for p in item["picks"] if p["stock_type"] == "Unpackaged"]
        receipt_qty = sum(int(p["take"]) for p in unpackaged_picks)

        if receipt_qty > 0:
            summary.append({
                "SKU": sku,
                "Product": row_match.iloc[0]["Standardized Full Name"],
                "Receipts to Print": receipt_qty
            })

    return pd.DataFrame(summary)


def parse_pick_line(line):
    m = re.match(r"\s*(\d{1,3})\s*[-–—:]?\s*(\d+)\s*unit", line, re.IGNORECASE)
    if not m:
        m = re.match(r"\s*(\d{1,3})\s+(\d+)\s*unit", line, re.IGNORECASE)
    if not m:
        return None
    return normalize_sku(m.group(1)), int(m.group(2))


st.title("Perfume Inventory & Pick Assistant")

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Pick Assistant",
    "Inventory Overview",
    "Add Stock",
    "Receipt Directory",
    "Manual Pick"
])

inventory_df = load_inventory()
inventory_df["SKU"] = inventory_df["SKU"].astype(str).str.zfill(3)

with tab1:
    st.subheader("Paste SKU pick list below")
    st.caption("Use SKU only going forward. Example: 005 - 2 units")
    raw_text = st.text_area("Pick list", height=300)

    if st.button("Generate Pick List") and raw_text.strip():
        results = []
        not_found = []
        pick_plan = []

        lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
        for line in lines:
            parsed = parse_pick_line(line)
            if not parsed:
                not_found.append(f"Could not read line format: {line}")
                continue

            sku, qty = parsed
            matched_rows = inventory_df[inventory_df["SKU"] == sku]
            if matched_rows.empty:
                not_found.append(f"SKU {sku} not found")
                continue

            matched_name = matched_rows.iloc[0]["Standardized Full Name"]
            picks = get_pick_locations(sku, qty, inventory_df)
            pick_plan.append({"sku": sku, "matched": matched_name, "qty": qty, "picks": picks})

            pick_from_text = "; ".join([
                f"{p['location']} (take {p['take']}, {p['stock_type']})"
                if p["location"] else f"SHORTAGE: {p['take']} units unavailable"
                for p in picks
            ])

            results.append({
                "SKU": sku,
                "Matched To": matched_name,
                "Qty": qty,
                "Pick From": pick_from_text
            })

        st.session_state["pick_plan"] = pick_plan
        st.session_state["pick_results"] = results
        st.session_state["pick_not_found"] = not_found

    pick_plan = st.session_state.get("pick_plan", [])
    results = st.session_state.get("pick_results", [])
    not_found = st.session_state.get("pick_not_found", [])

    if results:
        st.success(f"✅ {len(results)} item(s) matched and ready to pick")
        st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)

    if not_found:
        st.warning("Could not process the following:")
        for item in not_found:
            st.write(f"- {item}")

    if pick_plan:
        st.divider()

        receipt_summary_df = get_receipt_print_summary(pick_plan, inventory_df)
        if not receipt_summary_df.empty:
            st.info("Receipt print quantities")
            st.dataframe(receipt_summary_df, use_container_width=True, hide_index=True)

            for _, row in receipt_summary_df.iterrows():
                st.write(f"- {row['SKU']} - {row['Product']}: print {row['Receipts to Print']}")

        pdf_bytes, missing, included = generate_receipt_pdf_for_picks(pick_plan, inventory_df)

        if included:
            st.success("Receipt file ready for download: " + ", ".join(included))

        if missing:
            st.warning("No receipt found for SKU(s): " + ", ".join(missing))

        if pdf_bytes and included:
            st.download_button(
                "Download Printable Receipts PDF",
                data=pdf_bytes,
                file_name="receipts_to_print.pdf",
                mime="application/pdf",
                key="pick_receipt_download"
            )

    if pick_plan:
        st.divider()
        if st.button("Confirm picks and deduct inventory"):
            updated = inventory_df.copy()
            for item in pick_plan:
                for p in item["picks"]:
                    if p["location"] is None:
                        continue
                    mask = (
                        (updated["SKU"] == item["sku"]) &
                        (updated["Location"] == p["location"])
                    )
                    idxs = updated.index[mask].tolist()
                    if idxs:
                        i = idxs[0]
                        updated.at[i, "Qty"] = max(0, int(updated.at[i, "Qty"]) - int(p["take"]))

            save_inventory(updated)
            st.success("✅ Inventory updated from confirmed picks.")
            st.session_state["pick_plan"] = []
            st.session_state["pick_results"] = []
            st.session_state["pick_not_found"] = []
            st.rerun()

with tab2:
    st.subheader("Current Inventory")

    stock_filter = st.selectbox("Stock type", ["All", "Packaged", "Unpackaged"])
    location_filter = st.selectbox("Location", ["All"] + sorted(inventory_df["Location"].dropna().unique().tolist()))
    sku_filter = st.text_input("Search SKU")

    view_df = inventory_df.copy()

    if stock_filter != "All":
        view_df = view_df[view_df["Stock Type"] == stock_filter]
    if location_filter != "All":
        view_df = view_df[view_df["Location"] == location_filter]
    if sku_filter.strip():
        view_df = view_df[view_df["SKU"] == normalize_sku(sku_filter)]

    st.dataframe(
        view_df[["SKU", "Standardized Full Name", "Location", "Qty", "Stock Type", "Pick Priority"]],
        use_container_width=True,
        hide_index=True
    )

with tab3:
    st.subheader("Add newly arrived stock")

    sku_options = inventory_df[["SKU", "Standardized Full Name"]].drop_duplicates().sort_values(["SKU"])
    sku_choice = st.selectbox(
        "Select existing SKU",
        [f"{r.SKU} - {r['Standardized Full Name']}" for _, r in sku_options.iterrows()]
    )

    selected_sku = sku_choice.split(" - ")[0]
    selected_name = sku_options[sku_options["SKU"] == selected_sku].iloc[0]["Standardized Full Name"]

    new_qty = st.number_input("Quantity", min_value=1, step=1, value=1)

    location_options = [
        "Cabinet",
        "Location 1 - Next to cabinet",
        "Location 2 - On the table",
        "Location 3 - Next to wall",
        "Box 1",
        "Box 2",
        "Box 3",
        "Box 4",
        "Box 5",
        "Box 6",
        "Box 7",
        "Box 8",
        "Box 9",
        "Box 10",
        "Box 11",
        "Box 12",
        "Box 13",
        "Box 14",
        "Tub"
    ]

    new_location = st.selectbox("Location", location_options, index=0)
    new_type = st.selectbox("Stock type", ["Unpackaged", "Packaged"])

    default_priority = 99 if (new_type == "Unpackaged" and new_location == "Cabinet") else (1 if new_type == "Packaged" else 2)

    if st.button("Add Stock"):
        new_row = pd.DataFrame([{
            "SKU": selected_sku,
            "Location": new_location,
            "As Entered": selected_name,
            "Standardized Full Name": selected_name,
            "Qty": int(new_qty),
            "Needs Confirmation": False,
            "Stock Type": new_type,
            "Pick Priority": default_priority,
            "Status": "Confirmed"
        }])

        updated_df = pd.concat([inventory_df, new_row], ignore_index=True)
        save_inventory(updated_df)
        st.success(f"✅ Added {new_qty} unit(s) of SKU {selected_sku} to {new_location}.")
        st.rerun()

with tab4:
    st.subheader("Receipt Directory")
    st.caption("Receipts load automatically from the local receipts folder. You can also upload multiple PDF or DOCX receipts here using SKU-only filenames like 015.pdf or 015.docx")

    uploaded_files = st.file_uploader(
        "Upload receipt files",
        type=["pdf", "docx"],
        accept_multiple_files=True
    )

    if uploaded_files:
        if st.button("Save uploaded receipt files"):
            saved, skipped = save_uploaded_receipts(uploaded_files)
            if saved:
                st.success("✅ Saved to receipts folder: " + ", ".join(saved))
            if skipped:
                st.warning("Skipped: " + ", ".join(skipped))
            st.rerun()

    receipts_df = load_receipts_from_directory()

    if receipts_df.empty:
        st.info("No receipts found in the receipts folder.")
    else:
        display_df = receipts_df.copy()
        display_df["sku"] = display_df["sku"].astype(str).str.zfill(3)
        display_df["Matched Product"] = display_df["sku"].map(
            inventory_df.drop_duplicates("SKU").set_index("SKU")["Standardized Full Name"]
        ).fillna("Unmatched SKU")

        st.success(f"✅ {len(display_df)} receipt file(s) loaded automatically from the receipts folder.")
        st.dataframe(
            display_df[["sku", "Matched Product", "filename", "file_type"]],
            use_container_width=True,
            hide_index=True
        )

with tab5:
    st.subheader("Manual Pick")
    manual_sku = st.text_input("Enter SKU", key="manual_sku")

    if manual_sku.strip():
        sku = normalize_sku(manual_sku)
        product_rows = inventory_df[inventory_df["SKU"] == sku].copy()

        if product_rows.empty:
            st.warning("SKU not found.")
        else:
            selected_product = product_rows.iloc[0]["Standardized Full Name"]
            st.markdown(f"**{sku} - {selected_product}**")
            product_rows = product_rows.sort_values(["Pick Priority", "Location"])
            st.dataframe(
                product_rows[["Location", "Qty", "Stock Type", "Pick Priority"]],
                use_container_width=True,
                hide_index=True
            )

            total_available = int(product_rows["Qty"].sum())
            st.caption(f"Total available: {total_available} units")
            manual_qty = st.number_input("Quantity to pick", min_value=1, step=1, value=1, key="manual_qty")

            if st.button("Generate Manual Pick"):
                if manual_qty > total_available:
                    st.error(f"Not enough stock. Only {total_available} unit(s) available.")
                else:
                    picks = get_pick_locations(sku, int(manual_qty), inventory_df)
                    st.session_state["manual_pick_plan"] = [{
                        "sku": sku,
                        "matched": selected_product,
                        "qty": int(manual_qty),
                        "picks": picks
                    }]
                    st.success("✅ Manual pick generated.")

            manual_pick_plan = st.session_state.get("manual_pick_plan", [])

            if manual_pick_plan:
                col_pdf, col_confirm = st.columns(2)

                with col_pdf:
                    receipt_summary_df = get_receipt_print_summary(manual_pick_plan, inventory_df)
                    if not receipt_summary_df.empty:
                        st.info("Receipt print quantities")
                        st.dataframe(receipt_summary_df, use_container_width=True, hide_index=True)

                        for _, row in receipt_summary_df.iterrows():
                            st.write(f"- {row['SKU']} - {row['Product']}: print {row['Receipts to Print']}")

                    pdf_bytes, missing, included = generate_receipt_pdf_for_picks(manual_pick_plan, inventory_df)

                    if included:
                        st.success("Receipt file ready for download: " + ", ".join(included))

                    if missing:
                        st.warning("No receipt found for SKU(s): " + ", ".join(missing))

                    if pdf_bytes and included:
                        st.download_button(
                            "Download Printable Receipt PDF",
                            data=pdf_bytes,
                            file_name="manual_pick_receipt.pdf",
                            mime="application/pdf",
                            key="manual_pdf_download"
                        )

                with col_confirm:
                    if st.button("Confirm Pick and Deduct Inventory"):
                        updated = inventory_df.copy()
                        for item in manual_pick_plan:
                            for p in item["picks"]:
                                if p["location"] is None:
                                    continue
                                mask = (
                                    (updated["SKU"] == item["sku"]) &
                                    (updated["Location"] == p["location"])
                                )
                                idxs = updated.index[mask].tolist()
                                if idxs:
                                    i = idxs[0]
                                    updated.at[i, "Qty"] = max(0, int(updated.at[i, "Qty"]) - int(p["take"]))

                        save_inventory(updated)
                        st.success("✅ Inventory updated from manual pick.")
                        st.session_state["manual_pick_plan"] = []
                        st.rerun()
