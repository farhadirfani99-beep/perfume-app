import streamlit as st
import pandas as pd
import re
from pathlib import Path

INVENTORY_FILE = "inventory_master_final.csv"

st.set_page_config(page_title="Perfume Inventory & Pick Assistant", layout="wide")


def normalize_sku(text):
    text = str(text).strip().upper()
    if text.isdigit():
        return text.zfill(3)
    return text


def load_inventory():
    df = pd.read_csv(INVENTORY_FILE, dtype={"SKU": str})

    if df.empty:
        return pd.DataFrame(columns=[
            "SKU", "Location", "As Entered", "Standardized Full Name", "Qty",
            "Needs Confirmation", "Stock Type", "Pick Priority", "Status"
        ])

    df["SKU"] = df["SKU"].apply(normalize_sku)
    df["Qty"] = pd.to_numeric(df["Qty"], errors="coerce").fillna(0).astype(int)
    df["Pick Priority"] = pd.to_numeric(df["Pick Priority"], errors="coerce").fillna(999).astype(int)
    return df


def save_inventory(df):
    df = df.copy()
    df["SKU"] = df["SKU"].apply(normalize_sku)
    df["Qty"] = pd.to_numeric(df["Qty"], errors="coerce").fillna(0).astype(int)
    df["Pick Priority"] = pd.to_numeric(df["Pick Priority"], errors="coerce").fillna(999).astype(int)
    df.to_csv(INVENTORY_FILE, index=False)


def get_pick_locations(sku, qty_needed, df):
    sku = normalize_sku(sku)
    candidates = df[df["SKU"].apply(normalize_sku) == sku].copy()
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


def parse_pick_line(line):
    m = re.match(r"\s*([A-Za-z0-9\-]{1,15})\s*[-–—:]?\s*(\d+)\s*unit", line, re.IGNORECASE)
    if not m:
        m = re.match(r"\s*([A-Za-z0-9\-]{1,15})\s+(\d+)\s*unit", line, re.IGNORECASE)
    if not m:
        return None
    return normalize_sku(m.group(1)), int(m.group(2))


st.title("Perfume Inventory & Pick Assistant")

tab1, tab2, tab3, tab4 = st.tabs([
    "Pick Assistant",
    "Inventory Overview",
    "Add Stock",
    "Manual Pick"
])

inventory_df = load_inventory()

with tab1:
    st.subheader("Paste SKU pick list below")
    st.caption("SKU can be numbers, letters, or a mix. Example: 005 - 2 units or JAD01 - 2 units")
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
    st.subheader("Add Stock")

    add_mode = st.radio(
        "What are you adding?",
        ["Add to an existing SKU", "Add a brand new SKU"],
        key="add_mode"
    )

    location_options = [
        "Cabinet",
        "Location 1 - Next to cabinet",
        "Location 2 - On the table",
        "Location 3 - Next to wall",
        "Box 1", "Box 2", "Box 3", "Box 4", "Box 5", "Box 6", "Box 7",
        "Box 8", "Box 9", "Box 10", "Box 11", "Box 12", "Box 13", "Box 14",
        "Tub"
    ]

    if add_mode == "Add to an existing SKU":
        if inventory_df.empty:
            st.info("No existing SKUs found. Use 'Add a brand new SKU' instead.")
        else:
            sku_options = inventory_df[["SKU", "Standardized Full Name"]].drop_duplicates().sort_values(["SKU"])
            sku_choice = st.selectbox(
                "Select existing SKU",
                [f"{r.SKU} - {r['Standardized Full Name']}" for _, r in sku_options.iterrows()],
                key="existing_sku_choice"
            )

            selected_sku = sku_choice.split(" - ")[0]
            selected_name = sku_options[sku_options["SKU"] == selected_sku].iloc[0]["Standardized Full Name"]

            new_qty = st.number_input("Quantity", min_value=1, step=1, value=1, key="existing_qty")
            new_location = st.selectbox("Location", location_options, index=0, key="existing_location")
            new_type = st.selectbox("Stock type", ["Unpackaged", "Packaged"], key="existing_type")

            default_priority = 99 if (new_type == "Unpackaged" and new_location == "Cabinet") else (1 if new_type == "Packaged" else 2)

            if st.button("Add Stock", key="add_existing_btn"):
                new_row = pd.DataFrame([{
                    "SKU": normalize_sku(selected_sku),
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
                st.success(f"✅ Added {new_qty} unit(s) of SKU {normalize_sku(selected_sku)} to {new_location}.")
                st.rerun()

    else:
        st.caption("Enter a brand new SKU. Letters, numbers, or a mix are allowed, e.g. DIOR01 or 067")

        new_sku_input = st.text_input("New SKU", key="new_sku_input")
        new_product_name = st.text_input("Product name", key="new_product_name")
        new_qty = st.number_input("Quantity", min_value=1, step=1, value=1, key="new_qty")
        new_location = st.selectbox("Location", location_options, index=0, key="new_location")
        new_type = st.selectbox("Stock type", ["Unpackaged", "Packaged"], key="new_type")

        default_priority = 99 if (new_type == "Unpackaged" and new_location == "Cabinet") else (1 if new_type == "Packaged" else 2)

        if st.button("Add New SKU to Inventory", key="add_new_btn"):
            if not new_sku_input.strip() or not new_product_name.strip():
                st.error("Please enter both a SKU and a product name.")
            else:
                normalized_new_sku = normalize_sku(new_sku_input)

                if normalized_new_sku in inventory_df["SKU"].values:
                    st.warning(f"SKU {normalized_new_sku} already exists. Use 'Add to an existing SKU' instead, or choose a different SKU.")
                else:
                    new_row = pd.DataFrame([{
                        "SKU": normalized_new_sku,
                        "Location": new_location,
                        "As Entered": new_product_name.strip(),
                        "Standardized Full Name": new_product_name.strip(),
                        "Qty": int(new_qty),
                        "Needs Confirmation": False,
                        "Stock Type": new_type,
                        "Pick Priority": default_priority,
                        "Status": "Confirmed"
                    }])

                    updated_df = pd.concat([inventory_df, new_row], ignore_index=True)
                    save_inventory(updated_df)
                    st.success(f"✅ New SKU {normalized_new_sku} - {new_product_name.strip()} added with {new_qty} unit(s) at {new_location}.")
                    st.rerun()

with tab4:
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
