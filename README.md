# Perfume Inventory & Pick Assistant — Setup Guide

## What this app does
- Paste a pick list (from Claude) and it matches each item to your inventory using aliases + fuzzy matching.
- Always picks from PACKAGED stock first, then UNPACKAGED boxes.
- Items it can't find are shown as a clear warning list at the end.
- "Add Stock" tab auto-suggests matches to existing products to avoid duplicates (e.g. "Coco" vs "Chanel Coco").
- Runs online so you and your partner overseas can both access it from any browser.

## Step 1: Create a free Supabase project (this is your online database)
1. Go to supabase.com and sign up (free tier is enough).
2. Create a new project. Note your Project URL and anon/public API key (Settings > API).
3. In the SQL editor, run this to create your tables:

create table inventory (
  id bigint generated always as identity primary key,
  "Location" text,
  "As Entered" text,
  "Standardized Full Name" text,
  "Qty" int,
  "Needs Confirmation" boolean,
  "Stock Type" text,
  "Pick Priority" int,
  "Status" text
);

create table aliases (
  alias text primary key,
  canonical_name text
);

4. Import your starting inventory: use the Supabase Table Editor "Insert" > "Import CSV" and upload inventory_master_final.csv.

## Step 2: Deploy the app for free on Streamlit Community Cloud
1. Create a free GitHub account (if you don't have one) and a new repository.
2. Upload app.py and requirements.txt to that repository.
3. Go to share.streamlit.io, sign in with GitHub, and deploy your repository (select app.py as the entry file).
4. In the Streamlit app settings, go to "Secrets" and add:

SUPABASE_URL = "your-project-url"
SUPABASE_KEY = "your-anon-key"

5. Your app will get a public URL (e.g. yourapp.streamlit.app) that works from any device, including your partner's overseas.

## How you'll use it day to day
1. Give Claude your postage label PDF, get back a pick list.
2. Paste that list into the "Pick Assistant" tab.
3. Get an instant pick list sorted by packaged-first, with locations, plus a warning list of anything not found.
4. When new stock arrives, log it in "Add Stock" — it'll flag if it thinks the product already exists so you don't create duplicates.