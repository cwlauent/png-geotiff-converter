import streamlit as st
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import calculate_default_transform, reproject, Resampling
from PIL import Image
import numpy as np
import tempfile
import zipfile
import os
import io
import folium
from streamlit_folium import st_folium

# ---------------------- UI CONFIGURATION ----------------------
st.set_page_config(page_title="🗂️ Batch PNG ➜ GeoTIFF Converter", layout="wide")
st.title("🗺️ Batch PNG ➜ GeoTIFF Converter")

# ---------------------- SESSION DEFAULTS ----------------------
defaults = {
    "uploaded_files": [],
    "bounds_cache": {},
    "matched_keys": [],
    "png_files": {},
    "map_files": {},
    "zip_name": "converted_geotiffs.zip",
    "zip_data": None,
    "uploader_key": "uploader_initial",
    "conversion_summary": {}
}
for key, value in defaults.items():
    st.session_state.setdefault(key, value)

# ---------------------- SIDEBAR CONTROLS ----------------------
with st.sidebar:
    st.title("⚙️ Controls")

    if st.button("🔁 Reset App"):
        st.session_state.clear()
        st.session_state["uploader_key"] = os.urandom(8).hex()
        st.rerun()

    output_type = st.radio("GeoTIFF Output Type", ["Type 2 (Lat/Lon)", "Type 3 (Geocentric XYZ)"])

    uploaded_files = st.file_uploader(
        "📁 Upload PNG + .map files",
        type=["png", "map"],
        accept_multiple_files=True,
        key=st.session_state["uploader_key"]
    )

    if uploaded_files:
        st.markdown("### 📄 Imported Files")
        for f in uploaded_files:
            st.markdown(f"- `{f.name}`")

# ---------------------- APP INSTRUCTIONS ----------------------
with st.expander("📘 Instructions", expanded=True):
    st.markdown("""
    This tool allows you to batch convert georeferenced PNG images (with matching `.map` files) into a ZIP of GeoTIFFs.

    **How to Use:**
    1. Use the sidebar to upload `.png` + `.map` files.
    2. Choose the GeoTIFF output format.
    3. Click **Convert to GeoTIFF**.
    4. Download the results as a ZIP archive.
    """)

# ---------------------- FILE MATCHING ----------------------
def parse_map(file_contents):
    try:
        lines = file_contents.decode("utf-8").splitlines()
        coords = {}
        for line in lines:
            if line.startswith("MMPLL, 1"):
                coords["minx"], coords["maxy"] = map(float, line.split(",")[2:4])
            elif line.startswith("MMPLL, 2"):
                coords["maxx"], _ = map(float, line.split(",")[2:4])
            elif line.startswith("MMPLL, 3"):
                _, coords["miny"] = map(float, line.split(",")[2:4])
        return coords["minx"], coords["miny"], coords["maxx"], coords["maxy"]
    except Exception:
        return None

if uploaded_files:
    st.session_state.uploaded_files = uploaded_files
    st.session_state.png_files = {os.path.splitext(f.name)[0]: f for f in uploaded_files if f.name.lower().endswith('.png')}
    st.session_state.map_files = {os.path.splitext(f.name)[0]: f for f in uploaded_files if f.name.lower().endswith('.map')}
    matched = set(st.session_state.png_files) & set(st.session_state.map_files)
    st.session_state.matched_keys = sorted(matched)
    st.session_state.bounds_cache = {}

    if not matched:
        st.warning("⚠️ No matching PNG + MAP pairs found.")
    else:
        st.success(f"✅ Found {len(matched)} matched file pairs.")
        for key in st.session_state.matched_keys:
            try:
                st.session_state.map_files[key].seek(0)
                bounds = parse_map(st.session_state.map_files[key].read())
                if bounds:
                    st.session_state.bounds_cache[key] = bounds
                else:
                    st.error(f"❌ Invalid .map format: {key}.map")
            except Exception:
                st.error(f"❌ Failed to parse {key}.map")

# ---------------------- GEOREPROJECTION ----------------------
def reproject_to_xyz(src_path, dst_path):
    dst_crs = "EPSG:4978"
    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds)
        kwargs = src.meta.copy()
        kwargs.update({
            'crs': dst_crs,
            'transform': transform,
            'width': width,
            'height': height
        })
        with rasterio.open(dst_path, 'w', **kwargs) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=Resampling.nearest)

# ---------------------- CONVERT UI + LOGIC ----------------------
if st.session_state.bounds_cache:
    col1, col2 = st.columns([1, 2])
    with col1:
        if st.button("🚀 Convert to GeoTIFF"):
            zip_buffer = io.BytesIO()
            st.session_state.conversion_summary = {"success": [], "failed": [], "total_bytes": 0}
            progress = st.progress(0)
            status_slots = {key: st.empty() for key in st.session_state.matched_keys}

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                for i, key in enumerate(st.session_state.matched_keys):
                    try:
                        status_slots[key].info(f"🔄 {key}")
                        image = Image.open(st.session_state.png_files[key]).convert("RGBA")
                        img_array = np.array(image)
                        height, width = img_array.shape[:2]

                        bounds = st.session_state.bounds_cache[key]
                        transform = from_bounds(*bounds, width=width, height=height)
                        bands = [img_array[:, :, i] for i in range(4)]

                        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as temp_tif:
                            with rasterio.open(
                                temp_tif.name, 'w', driver='GTiff',
                                height=height, width=width, count=4,
                                dtype=bands[0].dtype,
                                crs="EPSG:4326", transform=transform
                            ) as dst:
                                for b in range(4):
                                    dst.write(bands[b], b + 1)
                            final_tif = temp_tif.name

                        if output_type == "Type 3 (Geocentric XYZ)":
                            temp_xyz = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                            reproject_to_xyz(final_tif, temp_xyz.name)
                            final_tif = temp_xyz.name

                        with open(final_tif, "rb") as f:
                            data = f.read()
                            zipf.writestr(f"{key}_geotiff.tif", data)
                            st.session_state.conversion_summary["total_bytes"] += len(data)

                        os.remove(final_tif)
                        if output_type == "Type 3 (Geocentric XYZ)":
                            os.remove(temp_xyz.name)

                        status_slots[key].success("✅ Success")
                        st.session_state.conversion_summary["success"].append(key)

                    except Exception as e:
                        status_slots[key].error(f"❌ {e}")
                        st.session_state.conversion_summary["failed"].append((key, str(e)))

                    progress.progress((i + 1) / len(st.session_state.matched_keys))

            st.session_state.zip_data = zip_buffer.getvalue()

    with col2:
        st.session_state.zip_name = st.text_input(
            "ZIP File Name",
            value=st.session_state.zip_name,
            help="This will be the name of the downloaded ZIP file."
        )

# ---------------------- DOWNLOAD + SUMMARY ----------------------
if st.session_state.zip_data:
    st.download_button(
        label="📦 Download ZIP",
        data=st.session_state.zip_data,
        file_name=st.session_state.zip_name if st.session_state.zip_name.endswith(".zip") else st.session_state.zip_name + ".zip",
        mime="application/zip"
    )

    st.subheader("✅ Convert Summary")
    summary = st.session_state.get("conversion_summary", {})
    success = summary.get("success", [])
    failed = summary.get("failed", [])
    total_bytes = summary.get("total_bytes", 0)
    size_mb = round(total_bytes / (1024 * 1024), 2)
    st.markdown(f"- ✅ **{len(success)}** succeeded")
    st.markdown(f"- ❌ **{len(failed)}** failed")
    st.markdown(f"- 📦 **{size_mb} MB** total GeoTIFF size")
    if failed:
        for key, error in failed:
            st.markdown(f"  - **{key}** — `{error}`")

# ---------------------- MAP PREVIEW ----------------------
if st.session_state.bounds_cache:
    st.divider()
    folium_map = folium.Map(zoom_start=2, control_scale=True)
    for key, bounds in st.session_state.bounds_cache.items():
        minx, miny, maxx, maxy = bounds
        coords = [[miny, minx], [miny, maxx], [maxy, maxx], [maxy, minx], [miny, minx]]
        folium.PolyLine(coords, color="blue", weight=2.5, tooltip=key).add_to(folium_map)

    first = next(iter(st.session_state.bounds_cache))
    bx = st.session_state.bounds_cache[first]
    folium_map.location = [(bx[1] + bx[3]) / 2, (bx[0] + bx[2]) / 2]

    st.subheader("🗺️ Map Preview of All Image Areas")
    st_folium(folium_map, height=500, width="100%")
