import streamlit as st
import pandas as pd
from rapidfuzz import process, fuzz
from supabase import create_client
import re

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
        sb.table("inventory").delete().neq("id", -1).execute()
        sb.table("inventory").insert(df.to_dict(orient="records")).execute()
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
    text = text.lower().strip()
    text = text.replace("’", "'")
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
        picks.append(f"{row['Location']} (take {take}, {row['Stock Type']})")
        remaining -= take
    if remaining > 0:
        picks.append(f"SHORTAGE: {remaining} units unavailable")
    return picks

st.title("Perfume & Cologne Inventory + Pick Assistant")
tab1, tab2, tab3 = st.tabs(["Pick Assistant", "Inventory Overview", "Add Stock"])

inventory_df = load_inventory()
aliases = load_aliases()
inv_names = inventory_df["Standardized Full Name"].dropna().astype(str).unique().tolist()

with tab1:
    st.subheader("Paste your pick list below")
    st.caption('Format: one item per line, e.g. "Coco Mademoiselle - 2 units (Rojas, Tiffen)"')
    raw_text = st.text_area("Pick list", height=250)

    if st.button("Generate Pick List") and raw_text.strip():
        lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
        results = []
        not_found = []

        for line in lines:
            m = re.match(r"(.+?)\s*[-–—]\s*(\d+)\s*unit", line, re.IGNORECASE)
            if not m:
                m = re.match(r"(.+?)\s*[-–—]\s*(\d+)", line)
            if not m:
                not_found.append(f"Could not read line format: {line}")
                continue
            name, qty = m.group(1).strip(), int(m.group(2))
            recip_m = re.search(r"\((.+?)\)", line)
            recipients = recip_m.group(1) if recip_m else ""

            matched, method = match_item(name, aliases, inv_names)
            if not matched:
                not_found.append(f"{name} ({qty} units) — {recipients}")
                continue

            picks = get_pick_locations(matched, qty, inventory_df)
            results.append({
                "Requested": name,
                "Matched To": matched,
                "Match Method": method,
                "Qty": qty,
                "Recipients": recipients,
                "Pick From": "; ".join(picks)
            })

        if results:
            st.success(f"{len(results)} item(s) matched and ready to pick")
            st.dataframe(pd.DataFrame(results), use_container_width=True)

        if not_found:
            st.warning("Could not find the following in inventory — please check manually:")
            for item in not_found:
                st.write(f"- {item}")

with tab2:
    st.subheader("Current Inventory")
    stock_filter = st.radio("Filter", ["All", "Packaged", "Unpackaged"], horizontal=True)
    view_df = inventory_df.copy()
    if stock_filter != "All":
        view_df = view_df[view_df["Stock Type"] == stock_filter]
    st.dataframe(view_df.sort_values(["Pick Priority", "Location"]), use_container_width=True)
    st.metric("Total units in stock", int(inventory_df["Qty"].sum()))

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
        updated_df = pd.concat([inventory_df, new_row], ignore_index=True)
        save_inventory(updated_df)
        if new_name and suggestion:
            save_alias(new_name, suggestion)
        st.success(f"Added {new_qty} unit(s) of '{final_name}' to {new_location}")
        st.rerun()