import streamlit as st
import pandas as pd
from supabase import create_client
import re
import io
import zipfile
from pathlib import Path

SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", "")

INVENTORY_FILE = "inventory_master_final.csv"
RECEIPTS_DIR = Path("receipts")
RECEIPTS_DIR.mkdir(exist_ok=True)

@st.cache_resource
def get_client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

sb = get_client() if SUPABASE_URL and SUPABASE_KEY else None
st.set_page_config(page_title="Perfume Inventory & Pick Assistant", layout="wide")


def normalize_sku(text):
    return str(text).strip().zfill(3)


def load_inventory():
    if sb:
        res = sb.table("inventory").select("*").execute()
        df = pd.DataFrame(res.data)
    else:
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

    if sb:
        rows = df.to_dict(orient="records")
        sb.table("inventory").delete().neq("id", -1).execute()
        if rows:
            sb.table("inventory").insert(rows).execute()
    else:
        df.to_csv(INVENTORY_FILE, index=False)


def get_receipt_path_for_sku(sku):
    sku = normalize_sku(sku)
    path = RECEIPTS_DIR / f"{sku}.docx"
    if path.exists():
        return path
    return None


def get_receipt_docx_bytes(sku):
    path = get_receipt_path_for_sku(sku)
    if not path:
        return None
    return path.read_bytes()


def list_receipts_from_folder():
    rows = []
    for file in RECEIPTS_DIR.glob("*.docx"):
        sku = normalize_sku(file.stem)
        rows.append({
            "SKU": sku,
            "Filename": file.name,
            "Type": "DOCX",
            "Path": str(file)
        })

    if not rows:
        return pd.DataFrame(columns=["SKU", "Filename", "Type", "Path"])

    return pd.DataFrame(rows).sort_values(["SKU", "Filename"]).reset_index(drop=True)


def build_receipt_zip(pick_plan, inventory_df):
    files = []
    missing = []

    for item in pick_plan:
        sku = normalize_sku(item["sku"])
        row_match = inventory_df[inventory_df["SKU"] == sku]
        if row_match.empty:
            continue

        if row_match.iloc[0]["Stock Type"] != "Unpackaged":
            continue

        docx_bytes = get_receipt_docx_bytes(sku)
        if not docx_bytes:
            missing.append(sku)
            continue

        name = row_match.iloc[0]["Standardized Full Name"]
        files.append({
            "sku": sku,
            "name": name,
            "qty": int(item["qty"]),
            "bytes": docx_bytes,
            "filename": f"{sku}.docx"
        })

    if not files:
        return None, missing, files

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.writestr(f["filename"], f["bytes"])
    buf.seek(0)
    return buf.getvalue(), missing, files


def get_pick_locations(sku, qty_needed, df):
    candidates = df[df["SKU"] == normalize_sku(sku)].copy()
    candidates = candidates.sort_values(["Pick Priority", "Location"])
    picks = []
    remaining = int(qty_needed)

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
        st.success(f"✅ {len(results)} item(s) matched and ready to pick")
        st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)

    if not_found:
        st.warning("Could not process the following:")
        for item in not_found:
            st.write(f"- {item}")

    if pick_plan:
        st.divider()
        zip_bytes, missing, docx_files = build_receipt_zip(pick_plan, inventory_df)

        if docx_files and zip_bytes:
            st.success("✅ Receipt ZIP ready for download.")
            st.download_button(
                label=f"Download all receipts ({len(docx_files)})",
                data=zip_bytes,
                file_name="receipts_to_print.zip",
                mime="application/zip"
            )

        if missing:
            st.warning("No DOCX receipt found for SKU(s): " + ", ".join(missing))

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
    st.caption("Receipts are loaded automatically from the receipts folder using SKU-only filenames like 018.docx")

    receipt_df = list_receipts_from_folder()

    if receipt_df.empty:
        st.info("No receipts found in the receipts folder.")
    else:
        receipt_df["Matched Product"] = receipt_df["SKU"].map(
            inventory_df.drop_duplicates("SKU").set_index("SKU")["Standardized Full Name"]
        ).fillna("Unmatched SKU")

        st.success(f"✅ {len(receipt_df)} receipt file(s) loaded automatically.")
        st.dataframe(
            receipt_df[["SKU", "Matched Product", "Filename", "Type"]],
            use_container_width=True,
            hide_index=True
        )

        selected_file = st.selectbox("Preview receipt file", receipt_df["Filename"].tolist())
        selected_row = receipt_df[receipt_df["Filename"] == selected_file].iloc[0]
        selected_path = Path(selected_row["Path"])

        with open(selected_path, "rb") as f:
            st.download_button(
                "Download selected receipt",
                data=f.read(),
                file_name=selected_path.name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
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
            st.dataframe(product_rows[["Location", "Qty", "Stock Type", "Pick Priority"]], use_container_width=True, hide_index=True)

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
                col_zip, col_confirm = st.columns(2)

                with col_zip:
                    zip_bytes, missing, docx_files = build_receipt_zip(manual_pick_plan, inventory_df)
                    if docx_files and zip_bytes:
                        st.success("✅ Receipt ZIP ready for download.")
                        st.download_button(
                            label=f"Download all receipts ({len(docx_files)})",
                            data=zip_bytes,
                            file_name="manual_pick_receipts.zip",
                            mime="application/zip",
                            key="manual_zip_download"
                        )
                    if missing:
                        st.warning("No DOCX receipt found for SKU(s): " + ", ".join(missing))

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
