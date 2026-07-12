import streamlit as st
import pandas as pd
from supabase import create_client
import re
import io
import os
import base64
from docx import Document
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm as mm_unit
from pypdf import PdfWriter, PdfReader

SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", "")

RECEIPT_WIDTH_MM = 80
RECEIPT_WIDTH_PT = RECEIPT_WIDTH_MM * mm_unit
LINE_HEIGHT = 12
FONT_SIZE = 9
MARGIN = 10

@st.cache_resource
def get_client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

sb = get_client() if SUPABASE_URL and SUPABASE_KEY else None
st.set_page_config(page_title="Perfume Inventory & Pick Assistant", layout="wide")

def load_inventory():
    if sb:
        res = sb.table("inventory").select("*").execute()
        return pd.DataFrame(res.data)
    return pd.read_csv("inventory_master_final.csv", dtype={"SKU": str})

def save_inventory(df):
    if sb:
        rows = df.to_dict(orient="records")
        sb.table("inventory").delete().neq("id", -1).execute()
        if rows:
            sb.table("inventory").insert(rows).execute()
    else:
        df.to_csv("inventory_master_final.csv", index=False)

def load_receipt_index():
    if not sb:
        return pd.DataFrame(columns=["sku", "filename", "file_base64"])
    res = sb.table("receipts").select("*").execute()
    return pd.DataFrame(res.data)

def normalize_sku(text):
    return str(text).strip().zfill(3)

def get_pick_locations(sku, qty_needed, df):
    candidates = df[df["SKU"].astype(str).str.zfill(3) == normalize_sku(sku)].copy()
    candidates = candidates.sort_values(["Pick Priority", "Location"])
    picks, remaining = [], qty_needed

    for _, row in candidates.iterrows():
        if remaining <= 0:
            break
        available = int(row["Qty"])
        if available <= 0:
            continue
        take = min(remaining, available)
        picks.append({
            "location": row["Location"],
            "take": take,
            "stock_type": row["Stock Type"],
            "sku": normalize_sku(row["SKU"]),
            "name": row["Standardized Full Name"]
        })
        remaining -= take

    if remaining > 0:
        picks.append({
            "location": None,
            "take": remaining,
            "stock_type": "SHORTAGE",
            "sku": normalize_sku(sku),
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

def get_receipt_bytes(sku):
    if not sb:
        return None
    res = sb.table("receipts").select("*").eq("sku", normalize_sku(sku)).execute()
    if not res.data:
        return None
    return base64.b64decode(res.data[0]["file_base64"])

def generate_receipt_pdf_for_picks(pick_plan, inventory_df):
    output_writer = PdfWriter()
    missing = []
    included = []

    for item in pick_plan:
        sku = normalize_sku(item["sku"])
        row_match = inventory_df[inventory_df["SKU"].astype(str).str.zfill(3) == sku]
        if row_match.empty:
            continue
        stock_type = row_match.iloc[0]["Stock Type"]
        if stock_type != "Unpackaged":
            continue

        docx_bytes = get_receipt_bytes(sku)
        if not docx_bytes:
            missing.append(sku)
            continue

        lines = extract_docx_lines(docx_bytes)
        pdf_bytes, _ = build_receipt_pdf_bytes(lines)
        reader = PdfReader(io.BytesIO(pdf_bytes))

        copies_needed = int(item["qty"])
        for _ in range(copies_needed):
            for page in reader.pages:
                output_writer.add_page(page)

        name = row_match.iloc[0]["Standardized Full Name"]
        included.append(f"{sku} - {name} x{copies_needed}")

    buf = io.BytesIO()
    output_writer.write(buf)
    return buf.getvalue(), missing, included

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

        lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
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
                f"{p['location']} (take {p['take']}, {p['stock_type']})" if p["location"] else f"SHORTAGE: {p['take']} units unavailable"
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
        st.success(f"{len(results)} item(s) matched and ready to pick")
        st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)

    if not_found:
        st.warning("Could not process the following:")
        for item in not_found:
            st.write(f"- {item}")

    if pick_plan:
        st.divider()
        if st.button("Generate Receipt PDFs for Unpackaged Items"):
            pdf_bytes, missing, included = generate_receipt_pdf_for_picks(pick_plan, inventory_df)
            if included:
                st.success("Included in PDF: " + ", ".join(included))
            if missing:
                st.warning("No receipt found for SKU(s): " + ", ".join(missing))
            if pdf_bytes and included:
                st.download_button(
                    "Download Printable Receipts PDF",
                    data=pdf_bytes,
                    file_name="receipts_to_print.pdf",
                    mime="application/pdf"
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
            st.success("Inventory updated from confirmed picks.")
            st.session_state["pick_plan"] = []
            st.session_state["pick_results"] = []
            st.session_state["pick_not_found"] = []
            st.rerun()

with tab2:
    st.subheader("Current Inventory")
    stock_filter = st.selectbox("Stock type", ["All", "Packaged", "Unpackaged"])
    location_filter = st.selectbox("Location", ["All"] + sorted(inventory_df["Location"].unique().tolist()))
    sku_filter = st.text_input("Search SKU")

    view_df = inventory_df.copy()
    if stock_filter != "All":
        view_df = view_df[view_df["Stock Type"] == stock_filter]
    if location_filter != "All":
        view_df = view_df[view_df["Location"] == location_filter]
    if sku_filter.strip():
        view_df = view_df[view_df["SKU"] == normalize_sku(sku_filter)]

    st.dataframe(view_df[["SKU", "Standardized Full Name", "Location", "Qty", "Stock Type"]], use_container_width=True, hide_index=True)

with tab3:
    st.subheader("Add newly arrived stock")
    sku_options = inventory_df[["SKU", "Standardized Full Name"]].drop_duplicates().sort_values(["SKU"])
    sku_choice = st.selectbox("Select existing SKU", [f"{r.SKU} - {r['Standardized Full Name']}" for _, r in sku_options.iterrows()])
    selected_sku = sku_choice.split(" - ")[0]
    selected_name = sku_options[sku_options["SKU"] == selected_sku].iloc[0]["Standardized Full Name"]
    new_qty = st.number_input("Quantity", min_value=1, step=1)
    new_location = st.text_input("Location (e.g. Box 15, Cabinet, Location 4)")
    new_type = st.selectbox("Stock type", ["Unpackaged", "Packaged"])

    if st.button("Add Stock"):
        new_row = pd.DataFrame([{
            "SKU": selected_sku,
            "Location": new_location,
            "As Entered": selected_name,
            "Standardized Full Name": selected_name,
            "Qty": int(new_qty),
            "Needs Confirmation": False,
            "Stock Type": new_type,
            "Pick Priority": 1 if new_type == "Packaged" else 2,
            "Status": "Confirmed"
        }])
        updated_df = pd.concat([inventory_df, new_row], ignore_index=True)
        save_inventory(updated_df)
        st.success(f"Added {new_qty} unit(s) of SKU {selected_sku} to {new_location}")
        st.rerun()

with tab4:
    st.subheader("Receipt Directory")
    st.caption("Receipts are now linked to SKU only.")
    uploaded = st.file_uploader("Upload receipt (.docx)", type=["docx"])
    receipt_sku = st.text_input("Link this receipt to SKU (3 digits)")

    if uploaded and receipt_sku and st.button("Save Receipt"):
        file_bytes = uploaded.read()
        encoded = base64.b64encode(file_bytes).decode("utf-8")
        if sb:
            sb.table("receipts").upsert({
                "sku": normalize_sku(receipt_sku),
                "filename": uploaded.name,
                "file_base64": encoded
            }).execute()
        st.success(f"Receipt saved and linked to SKU {normalize_sku(receipt_sku)}")
        st.rerun()

    if sb:
        res = sb.table("receipts").select("sku, filename").execute()
        if res.data:
            st.dataframe(pd.DataFrame(res.data), use_container_width=True, hide_index=True)
        else:
            st.info("No receipts uploaded yet.")

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
            st.dataframe(product_rows[["Location", "Qty", "Stock Type"]], use_container_width=True, hide_index=True)
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
                    st.success("Manual pick generated.")

            manual_pick_plan = st.session_state.get("manual_pick_plan", [])
            if manual_pick_plan:
                col_pdf, col_confirm = st.columns(2)
                with col_pdf:
                    if st.button("Generate Receipt PDF for This Pick"):
                        pdf_bytes, missing, included = generate_receipt_pdf_for_picks(manual_pick_plan, inventory_df)
                        if included:
                            st.success("Included in PDF: " + ", ".join(included))
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
                        st.success("Inventory updated from manual pick.")
                        st.session_state["manual_pick_plan"] = []
                        st.rerun()
