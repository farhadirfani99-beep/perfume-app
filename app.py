import streamlit as st
import pandas as pd
from rapidfuzz import process, fuzz
from supabase import create_client
import re
import io
import base64
from docx import Document
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm as mm_unit
from pypdf import PdfWriter, PdfReader

RECEIPT_WIDTH_MM = 80
RECEIPT_WIDTH_PT = RECEIPT_WIDTH_MM * mm_unit
LINE_HEIGHT = 12
FONT_SIZE = 9
MARGIN = 10

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

def get_receipt_bytes(product_name):
    if not sb:
        return None
    res = sb.table("receipts").select("*").eq("product_name", product_name).execute()
    if not res.data:
        return None
    return base64.b64decode(res.data[0]["file_base64"])

def generate_receipt_pdf_for_picks(pick_plan, inventory_df):
    output_writer = PdfWriter()
    missing = []
    included = []

    for item in pick_plan:
        matched_name = item["matched"]
        row_match = inventory_df[inventory_df["Standardized Full Name"] == matched_name]
        if row_match.empty:
            continue
        stock_type = row_match.iloc[0]["Stock Type"]
        if stock_type != "Unpackaged":
            continue

        docx_bytes = get_receipt_bytes(matched_name)
        if not docx_bytes:
            missing.append(matched_name)
            continue

        lines = extract_docx_lines(docx_bytes)
        pdf_bytes, _ = build_receipt_pdf_bytes(lines)
        reader = PdfReader(io.BytesIO(pdf_bytes))

        copies_needed = int(item["qty"])
        for _ in range(copies_needed):
            for page in reader.pages:
                output_writer.add_page(page)

        included.append(f"{matched_name} x{copies_needed}")

    buf = io.BytesIO()
    output_writer.write(buf)
    return buf.getvalue(), missing, included
    
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", "")

DEFAULT_ALIASES = {
    "sauvage elixir": "Dior Sauvage",
    "dior sauvage elixir": "Dior Sauvage",
    "savage elixir": "Dior Sauvage",
    "savage": "Dior Sauvage",
    "sauvage": "Dior Sauvage",
    "coco mademoiselle": "Chanel Coco Mademoiselle",
    "coco m": "Chanel Coco Mademoiselle",
    "coco noir": "Chanel Coco Noir",
    "coco n": "Chanel Coco Noir",
    "coco": "Coco",
    "j'adore": "Dior J'adore",
    "jadore": "Dior J'adore",
    "armani code": "Giorgio Armani Code",
    "code": "Giorgio Armani Code",
    "gucci bloom edp": "Bloom Perfum",
    "gucci bloom": "Bloom Perfum",
    "gc bloom edp": "Bloom Perfum",
    "gc bloom": "Bloom Perfum",
    "givenchy l'interdit": "Givenchy L'Interdit",
    "l'interdit": "Givenchy L'Interdit",
    "l'interdit givenchy": "Givenchy L'Interdit",
    "bvlgari man wood essence": "Bvlgari Man Wood Essence",
    "bvlgari wood essence": "Bvlgari Man Wood Essence",
    "ysl la nuit de l'homme": "Le Nuit",
    "la nuit de l'homme": "Le Nuit",
    "la nuit": "Le Nuit",
    "le nuit": "Le Nuit",
    "mancera red tobacco": "Reb Tobacco",
    "red tobacco": "Reb Tobacco",
    "reb tobacco": "Reb Tobacco",
    "myself": "Yves Saint Laurent MYSLF",
    "myslf": "Yves Saint Laurent MYSLF",
    "bdc edp": "Bleu de Chanel EDP",
    "bdc edt": "Bleu de Chanel EDT",
    "eros flame": "Versace Eros Flame",
}

@st.cache_resource
def get_client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

sb = get_client() if SUPABASE_URL and SUPABASE_KEY else None
st.set_page_config(page_title="Perfume Inventory & Pick Assistant", layout="wide")

def load_inventory():
    if sb:
        res = sb.table("inventory").select("*").execute()
        return pd.DataFrame(res.data)
    return pd.read_csv("inventory_master_final.csv")

def save_inventory(df):
    if sb:
        rows = df.to_dict(orient="records")
        sb.table("inventory").delete().neq("id", -1).execute()
        if rows:
            sb.table("inventory").insert(rows).execute()
    else:
        df.to_csv("inventory_master_final.csv", index=False)

def load_aliases():
    aliases = DEFAULT_ALIASES.copy()
    if sb:
        res = sb.table("aliases").select("*").execute()
        for row in res.data:
            aliases[row["alias"].lower().strip()] = row["canonical_name"]
    return aliases

def save_alias(alias, canonical_name):
    alias = alias.lower().strip()
    if sb:
        sb.table("aliases").upsert({"alias": alias, "canonical_name": canonical_name}).execute()

def normalize_text(text):
    text = str(text).lower().strip()
    text = text.replace("'", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def build_inventory_lookup(inv_names):
    lookup = {}
    for name in inv_names:
        lookup[normalize_text(name)] = name
    return lookup

def match_item(raw_name, aliases, inv_names, threshold=72):
    raw = normalize_text(raw_name)
    inv_lookup = build_inventory_lookup(inv_names)
    if raw in aliases:
        canon = aliases[raw]
        if canon in inv_names:
            return canon, "alias exact"
    if raw in inv_lookup:
        return inv_lookup[raw], "normalized exact"
    for alias, canon in aliases.items():
        if raw == normalize_text(alias):
            return canon, "alias normalized"
    best = process.extractOne(raw, list(inv_lookup.keys()), scorer=fuzz.WRatio)
    if best and best[1] >= threshold:
        return inv_lookup[best[0]], f"fuzzy ({best[1]:.0f}%)"
    alias_best = process.extractOne(raw, list(aliases.keys()), scorer=fuzz.WRatio)
    if alias_best and alias_best[1] >= threshold:
        return aliases[alias_best[0]], f"alias fuzzy ({alias_best[1]:.0f}%)"
    return None, "no match"

def get_pick_locations(matched_name, qty_needed, df):
    candidates = df[df["Standardized Full Name"] == matched_name].copy()
    candidates = candidates.sort_values(["Pick Priority", "Location"])
    picks, remaining = [], qty_needed
    for _, row in candidates.iterrows():
        if remaining <= 0:
            break
        available = int(row["Qty"])
        if available <= 0:
            continue
        take = min(remaining, available)
        picks.append({"location": row["Location"], "take": take, "stock_type": row["Stock Type"], "name": matched_name})
        remaining -= take
    if remaining > 0:
        picks.append({"location": None, "take": remaining, "stock_type": "SHORTAGE", "name": matched_name})
    return picks

def brand_from_name(name):
    n = str(name).lower()
    if "dior" in n:
        return "Dior"
    if "chanel" in n:
        return "Chanel"
    if "ysl" in n or "yves saint laurent" in n:
        return "YSL"
    if "armani" in n:
        return "Armani"
    if "tom ford" in n:
        return "Tom Ford"
    if "bvlgari" in n:
        return "Bvlgari"
    if "versace" in n:
        return "Versace"
    if "givenchy" in n:
        return "Givenchy"
    if "creed" in n:
        return "Creed"
    if "paco rabanne" in n or "1 million" in n:
        return "Paco Rabanne"
    if "gucci" in n:
        return "Gucci"
    if "louis vuitton" in n or n.startswith("lv "):
        return "Louis Vuitton"
    if "mancera" in n:
        return "Mancera"
    return "Other"

st.title("Perfume & Cologne Inventory + Pick Assistant")
tab1, tab2, tab3 = st.tabs(["Pick Assistant", "Inventory Overview", "Add Stock"])

inventory_df = load_inventory()
aliases = load_aliases()
inv_names = inventory_df["Standardized Full Name"].dropna().astype(str).unique().tolist()

with tab1:
    st.subheader("Paste your pick list below")
    st.caption(
        "Domestic orders: one item per line, e.g. \"Coco Mademoiselle - 2 units (Rojas, Tiffen)\". "
        "International orders: paste after a blank line, e.g. \"Sauvage Elixir 1 unit\" (no dash or recipients needed)."
    )
    raw_text = st.text_area("Pick list", height=300)

    def parse_line(line, order_type):
        m = re.match(r"(.+?)\s*[-–—]\s*(\d+)\s*unit", line, re.IGNORECASE)
        if not m:
            m = re.match(r"(.+?)\s*[-–—]\s*(\d+)", line)
        if m:
            name, qty = m.group(1).strip(), int(m.group(2))
            recip_m = re.search(r"\((.+?)\)", line)
            recipients = recip_m.group(1) if recip_m else ""
            return name, qty, recipients
        m = re.match(r"(.+?)\s+(\d+)\s*unit", line, re.IGNORECASE)
        if m:
            name, qty = m.group(1).strip(), int(m.group(2))
            recip_m = re.search(r"\((.+?)\)", line)
            recipients = recip_m.group(1) if recip_m else ("International" if order_type == "International" else "")
            return name, qty, recipients
        return None

    if st.button("Generate Pick List") and raw_text.strip():
        blocks = re.split(r"\n\s*\n", raw_text.strip())
        results = []
        not_found = []
        pick_plan = []

        for block_index, block in enumerate(blocks):
            order_type = "Domestic" if block_index == 0 else "International"
            lines = [l.strip() for l in block.split("\n") if l.strip()]

            for line in lines:
                parsed = parse_line(line, order_type)
                if not parsed:
                    not_found.append(f"Could not read line format: {line}")
                    continue
                name, qty, recipients = parsed
                matched, method = match_item(name, aliases, inv_names)
                if not matched:
                    not_found.append(f"{name} ({qty} units) — {recipients or order_type}")
                    continue
                picks = get_pick_locations(matched, qty, inventory_df)
                pick_plan.append({"requested": name, "matched": matched, "qty": qty, "picks": picks})
                pick_from_text = "; ".join([
                    f"{p['location']} (take {p['take']}, {p['stock_type']})"
                    if p["location"] else f"SHORTAGE: {p['take']} units unavailable"
                    for p in picks
                ])
                results.append({
                    "Order Type": order_type,
                    "Requested": name,
                    "Matched To": matched,
                    "Match Method": method,
                    "Qty": qty,
                    "Recipients": recipients,
                    "Pick From": pick_from_text
                })

        if results:
            st.success(f"{len(results)} item(s) matched and ready to pick")
            domestic_results = [r for r in results if r["Order Type"] == "Domestic"]
            international_results = [r for r in results if r["Order Type"] == "International"]
            if domestic_results:
                st.markdown("#### Domestic Orders")
                st.dataframe(pd.DataFrame(domestic_results), use_container_width=True, hide_index=True)
            if international_results:
                st.markdown("#### International Orders")
                st.dataframe(pd.DataFrame(international_results), use_container_width=True, hide_index=True)

        if not_found:
            st.warning("Could not find the following in inventory — please check manually:")
            for item in not_found:
                st.write(f"- {item}")

        if results:
            st.divider()
            if st.button("Confirm picks and deduct inventory"):
                updated = inventory_df.copy()
                for item in pick_plan:
                    for p in item["picks"]:
                        if p["location"] is None:
                            continue
                        mask = (
                            (updated["Standardized Full Name"] == item["matched"]) &
                            (updated["Location"] == p["location"])
                        )
                        idxs = updated.index[mask].tolist()
                        if idxs:
                            i = idxs[0]
                            updated.at[i, "Qty"] = max(0, int(updated.at[i, "Qty"]) - int(p["take"]))
                save_inventory(updated.drop(columns=["Brand"], errors="ignore"))
                st.success("Inventory updated from confirmed picks.")
                st.rerun()
                with tab2:st.subheader("Current Inventory")

    inventory_df["Brand"] = inventory_df["Standardized Full Name"].apply(brand_from_name)

    col1, col2, col3 = st.columns(3)
    with col1:
        stock_filter = st.selectbox("Stock type", ["All", "Packaged", "Unpackaged"])
    with col2:
        brand_filter = st.selectbox("Brand", ["All"] + sorted(inventory_df["Brand"].unique().tolist()))
    with col3:
        location_filter = st.selectbox("Location", ["All"] + sorted(inventory_df["Location"].unique().tolist()))

    view_df = inventory_df.copy()
    if stock_filter != "All":
        view_df = view_df[view_df["Stock Type"] == stock_filter]
    if brand_filter != "All":
        view_df = view_df[view_df["Brand"] == brand_filter]
    if location_filter != "All":
        view_df = view_df[view_df["Location"] == location_filter]

    total_units = int(view_df["Qty"].sum())
    total_lines = len(view_df)
    st.metric("Visible stock units", total_units)
    st.caption(f"{total_lines} stock lines shown")

    grouped = view_df.sort_values(["Pick Priority", "Location", "Brand", "Standardized Full Name"]).groupby("Location")

    for location, group in grouped:
        location_total = int(group["Qty"].sum())
        packaged_count = int(group[group["Stock Type"] == "Packaged"]["Qty"].sum())
        unpackaged_count = int(group[group["Stock Type"] == "Unpackaged"]["Qty"].sum())

        with st.container(border=True):
            st.markdown(f"### {location}")
            st.caption(f"Total units: {location_total} | Packaged: {packaged_count} | Unpackaged: {unpackaged_count}")
            display_group = group[["Standardized Full Name", "Brand", "Qty", "Stock Type", "As Entered"]].rename(
                columns={"Standardized Full Name": "Product", "Qty": "Units", "Stock Type": "Type"}
            )
            st.dataframe(display_group, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Look Up Stock & Adjust Quantity")

    search_term = st.text_input("Search by product name")
    lookup_df = inventory_df.copy()
    if search_term:
        norm_search = normalize_text(search_term)
        lookup_df = lookup_df[
            lookup_df["Standardized Full Name"].apply(lambda x: norm_search in normalize_text(x))
            | lookup_df["As Entered"].apply(lambda x: norm_search in normalize_text(x))
        ]

    if search_term and lookup_df.empty:
        st.warning("No matching products found in inventory.")
    elif search_term:
        lookup_df = lookup_df.reset_index(drop=True)
        lookup_df["Row Label"] = lookup_df.apply(
            lambda r: f"{r['Location']} | {r['Standardized Full Name']} | {r['Stock Type']} | {r['Qty']} units",
            axis=1
        )
        selected_label = st.selectbox("Select the exact stock line to adjust", lookup_df["Row Label"].tolist())
        selected_row = lookup_df[lookup_df["Row Label"] == selected_label].iloc[0]
        original_index = inventory_df[
            (inventory_df["Location"] == selected_row["Location"]) &
            (inventory_df["Standardized Full Name"] == selected_row["Standardized Full Name"]) &
            (inventory_df["Stock Type"] == selected_row["Stock Type"])
        ].index[0]

        current_qty = int(inventory_df.at[original_index, "Qty"])
        st.write(f"**Current quantity at {selected_row['Location']}:** {current_qty} units")

        adjust_col1, adjust_col2, adjust_col3 = st.columns(3)
        with adjust_col1:
            add_amount = st.number_input("Add units", min_value=0, step=1, value=0, key="add_units")
            if st.button("Add Stock to This Location"):
                updated = inventory_df.drop(columns=["Brand"], errors="ignore").copy()
                updated.at[original_index, "Qty"] = current_qty + int(add_amount)
                save_inventory(updated)
                st.success(f"Added {add_amount} unit(s). New quantity: {current_qty + int(add_amount)}")
                st.rerun()

        with adjust_col2:
            remove_amount = st.number_input("Remove units", min_value=0, step=1, value=0, key="remove_units")
            if st.button("Remove Stock from This Location"):
                new_qty = max(0, current_qty - int(remove_amount))
                updated = inventory_df.drop(columns=["Brand"], errors="ignore").copy()
                updated.at[original_index, "Qty"] = new_qty
                save_inventory(updated)
                st.success(f"Removed {remove_amount} unit(s). New quantity: {new_qty}")
                st.rerun()

        with adjust_col3:
            set_amount = st.number_input("Set exact quantity", min_value=0, step=1, value=current_qty, key="set_units")
            if st.button("Set Exact Quantity"):
                updated = inventory_df.drop(columns=["Brand"], errors="ignore").copy()
                updated.at[original_index, "Qty"] = int(set_amount)
                save_inventory(updated)
                st.success(f"Quantity set to {int(set_amount)}")
                st.rerun()

    st.divider()
    st.subheader("Edit or Delete Inventory Item")

    inventory_df = inventory_df.reset_index(drop=True)
    inventory_df["Edit Label"] = inventory_df.apply(
        lambda r: f"{r['Location']} | {r['Standardized Full Name']} | {r['Qty']} units", axis=1
    )
    selected_edit_label = st.selectbox("Select item to edit", inventory_df["Edit Label"].tolist())
    selected_edit_row = inventory_df[inventory_df["Edit Label"] == selected_edit_label].iloc[0]
    selected_edit_index = selected_edit_row.name

    edit_location = st.text_input("Location", value=str(selected_edit_row["Location"]))
    edit_as_entered = st.text_input("As Entered", value=str(selected_edit_row["As Entered"]))
    edit_full_name = st.text_input("Standardized Full Name", value=str(selected_edit_row["Standardized Full Name"]))
    edit_qty = st.number_input("Quantity", min_value=0, step=1, value=int(selected_edit_row["Qty"]))
    edit_stock_type = st.selectbox(
        "Stock Type", ["Packaged", "Unpackaged"],
        index=0 if str(selected_edit_row["Stock Type"]) == "Packaged" else 1
    )

    col_save, col_delete = st.columns(2)
    with col_save:
        if st.button("Save Changes"):
            updated = inventory_df.drop(columns=["Edit Label", "Brand"], errors="ignore").copy()
            updated.at[selected_edit_index, "Location"] = edit_location
            updated.at[selected_edit_index, "As Entered"] = edit_as_entered
            updated.at[selected_edit_index, "Standardized Full Name"] = edit_full_name
            updated.at[selected_edit_index, "Qty"] = int(edit_qty)
            updated.at[selected_edit_index, "Stock Type"] = edit_stock_type
            updated.at[selected_edit_index, "Pick Priority"] = 1 if edit_stock_type == "Packaged" else 2
            save_inventory(updated)
            st.success("Inventory item updated.")
            st.rerun()

    with col_delete:
        if st.button("Delete Item"):
            updated = inventory_df.drop(columns=["Edit Label", "Brand"], errors="ignore").copy()
            updated = updated.drop(index=selected_edit_index).reset_index(drop=True)
            save_inventory(updated)
            st.success("Inventory item deleted.")
            st.rerun()

with tab3:
    st.subheader("Add newly arrived stock")
    new_name = st.text_input("Product name (as written on box/invoice)")
    new_qty = st.number_input("Quantity", min_value=1, step=1)
    new_location = st.text_input("Location (e.g. Box 15, Cabinet, Location 4)")
    new_type = st.selectbox("Stock type", ["Unpackaged", "Packaged"])

    suggestion, method = (None, None)
    confirm_match = False
    if new_name:
        suggestion, method = match_item(new_name, aliases, inv_names)
        if suggestion:
            st.info(f"This looks like an existing product: **{suggestion}** (match: {method})")
            confirm_match = st.checkbox(f"Yes, add to existing '{suggestion}' instead of creating a new product")
        else:
            st.warning("No close match found — this will be added as a brand new product.")

    if st.button("Add Stock"):
        final_name = suggestion if (new_name and suggestion and confirm_match) else new_name
        new_row = pd.DataFrame([{
            "Location": new_location,
            "As Entered": new_name,
            "Standardized Full Name": final_name,
            "Qty": int(new_qty),
            "Needs Confirmation": False,
            "Stock Type": new_type,
            "Pick Priority": 1 if new_type == "Packaged" else 2,
            "Status": "Confirmed"
        }])
        updated_df = pd.concat([inventory_df.drop(columns=["Brand", "Edit Label"], errors="ignore"), new_row], ignore_index=True)
        save_inventory(updated_df)
        if new_name and suggestion:
            save_alias(new_name, suggestion)
        st.success(f"Added {new_qty} unit(s) of '{final_name}' to {new_location}")
        st.rerun()
