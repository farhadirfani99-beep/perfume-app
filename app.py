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
        picks.append({
            "location": row["Location"],
            "take": take,
            "stock_type": row["Stock Type"],
            "name": matched_name
        })
        remaining -= take

    if remaining > 0:
        picks.append({
            "location": None,
            "take": remaining,
            "stock_type": "SHORTAGE",
            "name": matched_name
        })

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
                pick_plan.append({
                    "requested": name,
                    "matched": matched,
                    "qty": qty,
                    "picks": picks
                })

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
                            updated.at[i, "Qty"] = max(
                                0,
                                int(updated.at[i, "Qty"]) - int(p["take"])
                            )

                save_inventory(updated.drop(columns=["Brand"], errors="ignore"))
                st.success("Inventory updated from confirmed picks.")
                st.rerun()
