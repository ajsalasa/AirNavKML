#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Airway Builder — Crea aerovías interactivamente y exporta a KML/CSV
Requisitos:  pip install streamlit pandas

Ejecuta:  streamlit run airway_builder.py
"""

import math
import re
import io
import os
import argparse
import pandas as pd
import streamlit as st

# =========================
# Utilidades de coordenadas
# =========================

# =========================
# Distancias y destinos
# =========================

EARTH_R_M = 6371000.0  # radio medio esférico

def normalize_lon_deg(lon_deg):
    """Normaliza longitud a [-180, 180)."""
    return (lon_deg + 180.0) % 360.0 - 180.0

def gc_distance_m(lat1, lon1, lat2, lon2, R=EARTH_R_M):
    """Distancia gran círculo (haversine)."""
    φ1, λ1, φ2, λ2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dφ = φ2 - φ1
    dλ = λ2 - λ1
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    c = 2*math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def meters_to_units(m, units):
    """Convierte metros a ft/m/FL (devuelve número para m/ft y 'FLxxx' para fl)."""
    if m is None:
        return None
    if units == "m":
        return round(float(m), 2)
    if units == "ft":
        return round(float(m) / 0.3048, 1)
    if units == "fl":
        ft = float(m) / 0.3048
        fl = round(ft / 100.0)
        return f"FL{int(fl):03d}"
    return m

def rhumb_distance_m(lat1, lon1, lat2, lon2, R=EARTH_R_M):
    """Distancia loxodrómica (rumbo constante)."""
    φ1, λ1, φ2, λ2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dφ = φ2 - φ1
    dλ = λ2 - λ1
    # Ajuste si cruza antimeridiano
    if abs(dλ) > math.pi:
        dλ = dλ - math.copysign(2*math.pi, dλ)
    Δψ = math.log(math.tan(math.pi/4 + φ2/2) / math.tan(math.pi/4 + φ1/2)) if φ2 != φ1 else 0.0
    q = dφ/Δψ if abs(Δψ) > 1e-12 else math.cos(φ1)
    δ = math.sqrt(dφ*dφ + (q*dλ)*(q*dλ))
    return δ * R

def destination_gc(lat1, lon1, bearing_deg, distance_m, R=EARTH_R_M):
    """Destino gran círculo dado rumbo inicial y distancia."""
    θ = math.radians(bearing_deg)
    δ = distance_m / R
    φ1 = math.radians(lat1)
    λ1 = math.radians(lon1)
    φ2 = math.asin(math.sin(φ1)*math.cos(δ) + math.cos(φ1)*math.sin(δ)*math.cos(θ))
    λ2 = λ1 + math.atan2(math.sin(θ)*math.sin(δ)*math.cos(φ1),
                         math.cos(δ) - math.sin(φ1)*math.sin(φ2))
    return math.degrees(φ2), normalize_lon_deg(math.degrees(λ2))

def destination_rhumb(lat1, lon1, bearing_deg, distance_m, R=EARTH_R_M):
    """Destino loxodrómico (rumbo constante) dado rumbo y distancia."""
    θ = math.radians(bearing_deg)
    φ1 = math.radians(lat1)
    λ1 = math.radians(lon1)
    δ = distance_m / R
    dφ = δ * math.cos(θ)
    φ2 = φ1 + dφ
    # Manejo cercano a polos
    if abs(φ2) > math.pi/2:
        φ2 = math.copysign(math.pi/2 - 1e-12, φ2)
    Δψ = math.log(math.tan(math.pi/4 + φ2/2) / math.tan(math.pi/4 + φ1/2)) if φ2 != φ1 else 0.0
    q = dφ/Δψ if abs(Δψ) > 1e-12 else math.cos(φ1)
    dλ = δ * math.sin(θ) / q
    λ2 = λ1 + dλ
    return math.degrees(φ2), normalize_lon_deg(math.degrees(λ2))

def _to_float(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", ".")
    try:
        return float(s)
    except:
        return None

def _parse_alt_value(v, units="m"):
    """Convierte a metros desde m/ft/FL. Acepta strings tipo 'FL180'."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v).strip().upper()
    if units == "fl":
        s = s.replace("FL", "").strip()
        n = _to_float(s)
        return None if n is None else n * 100.0 * 0.3048
    n = _to_float(s)
    if n is None:
        if s.endswith("FT"):
            n = _to_float(s[:-2])
            return None if n is None else n * 0.3048
        if s.startswith("FL"):
            n = _to_float(s[2:])
            return None if n is None else n * 100.0 * 0.3048
        return None
    if units == "ft":
        return n * 0.3048
    return n

def detect_coord_columns(df):
    cols = list(df.columns)
    low = [c.lower() for c in cols]
    lat = [i for i,c in enumerate(low) if "lat" in c]
    lon = [i for i,c in enumerate(low) if ("lon" in c) or ("long" in c)]
    combo = [i for i,c in enumerate(low) if ("coord" in c) or ("wgs" in c) or ("geog" in c)]
    combo += [i for i,c in enumerate(low) if c in ("position","pos","location")]
    lat_col = cols[lat[0]] if lat else None
    lon_col = cols[lon[0]] if lon else None
    combo_col = cols[combo[0]] if combo else None
    name_col = None
    for prefer in ["name","id","fix","wpt"]:
        if prefer in low:
            name_col = cols[low.index(prefer)]
            break
    alt_col = None
    for prefer in ["alt","altura","nivel"]:
        if prefer in low:
            alt_col = cols[low.index(prefer)]
            break
    type_col = None
    for prefer in ["type","tipo"]:
        if prefer in low:
            type_col = cols[low.index(prefer)]
            break
    return lat_col, lon_col, combo_col, name_col, alt_col, type_col

def parse_dms_piece(piece):
    piece = re.sub(r"[NSEW]", "", str(piece), flags=re.I).strip().replace("º","°")
    piece = re.sub(r"[°]", " ", piece)
    piece = piece.replace("'", " ").replace('"', " ").replace("’"," ").replace("′"," ")
    toks = [t for t in re.split(r"[\s:]+", piece.strip()) if t]
    if len(toks) == 3:
        deg, mi, se = float(toks[0]), float(toks[1]), float(toks[2])
    elif len(toks) == 2:
        deg, mi, se = float(toks[0]), float(toks[1]), 0.0
    elif len(toks) == 1:
        val = _to_float(toks[0])
        if val is None:
            raise ValueError("DMS inválido")
        return val
    else:
        raise ValueError("DMS inválido")
    return deg + mi/60.0 + se/3600.0

def parse_compact_dms(piece, guess_lon=False):
    """
    Formatos: DDMMSS.SN / DDDMMSS.SW (compacto aeronáutico)
    """
    s = str(piece).strip().upper()
    m = re.match(r"^([0-9.]+)\s*([NSEW])$", s)
    m2 = re.match(r"^([NSEW])\s*([0-9.]+)$", s)
    hem = None; core = None
    if m:
        core, hem = m.group(1), m.group(2)
    elif m2:
        hem, core = m2.group(1), m2.group(2)
    else:
        core = re.sub(r"[^0-9.]", "", s)
    if not core:
        raise ValueError("Compact DMS inválido")
    if "." in core:
        left, frac = core.split(".", 1); frac = "." + frac
    else:
        left, frac = core, ""
    deg_len = 2 if len(left) in (6,7) else 3 if len(left) in (7,8,9) else (3 if guess_lon else 2)
    if len(left) < deg_len+4:
        left = left.rjust(deg_len+4, "0")
    deg = float(left[:deg_len])
    mm = float(left[deg_len:deg_len+2])
    ss = float(left[deg_len+2:deg_len+4])
    if frac:
        ss = float(f"{int(ss)}{frac}")
    val = deg + mm/60.0 + ss/3600.0
    if hem in ("S","W"):
        val = -val
    return val

def to_decimal(coord_str, context="lat"):
    """
    Acepta: decimal, DMS (10°05'30.2"N), compacto (DDMMSS.SN / DDDMMSS.SW)
    También acepta 'lat,lon' y devuelve tupla ("PAIR", lat, lon).
    """
    if coord_str is None or (isinstance(coord_str, float) and math.isnan(coord_str)):
        return None
    if isinstance(coord_str, (int, float)):
        return float(coord_str)
    s = str(coord_str).strip().replace(",", ".")
    try:
        if not re.search(r"[NSEW]$", s, flags=re.I):
            return float(s)
    except:
        pass
    # 'lat, lon'
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
        if len(parts) == 2 and re.search(r"\d", parts[0]) and re.search(r"\d", parts[1]):
            la = to_decimal(parts[0], "lat"); lo = to_decimal(parts[1], "lon")
            return ("PAIR", la, lo)
    # DMS
    if re.search(r"[°'\"′]|:", s):
        val = parse_dms_piece(s)
        if re.search(r"[SW]", s, flags=re.I): val = -abs(val)
        if re.search(r"[NE]", s, flags=re.I): val = abs(val)
        return val
    # Compacto
    try:
        return parse_compact_dms(s, guess_lon=(context=="lon"))
    except:
        pass
    # fallback
    s2 = re.sub(r"[^0-9\.\-]", "", s)
    try:
        return float(s2)
    except:
        return None

# =========================
# Bearings / rumbo
# =========================

def initial_bearing_true(lat1, lon1, lat2, lon2):
    """
    Rumbo inicial verdadero (0-360) de punto1 a punto2, con fórmula de gran círculo.
    Entradas en grados decimales.
    """
    if None in (lat1, lon1, lat2, lon2):
        return None
    φ1, λ1, φ2, λ2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dλ = λ2 - λ1
    y = math.sin(dλ) * math.cos(φ2)
    x = math.cos(φ1)*math.sin(φ2) - math.sin(φ1)*math.cos(φ2)*math.cos(dλ)
    θ = math.atan2(y, x)
    brng = (math.degrees(θ) + 360.0) % 360.0
    return brng

# =========================
# KML helpers
# =========================

def kml_color_from_hex(hex_str, alpha="ff"):
    s = str(hex_str).strip().lstrip("#")
    if len(s) == 8:  # asume AABBGGRR
        return s.lower()
    if len(s) != 6:
        return "ff0000ff"  # rojo por defecto
    rr, gg, bb = s[0:2], s[2:4], s[4:6]
    return (alpha + bb + gg + rr).lower()

def alt_to_meters(val, units):
    """
    Convierte altitud a metros. units: "m" | "ft" | "fl".
    Acepta strings tipo "FL120" o "7500 ft".
    """
    if val is None:
        return 0.0
    s = str(val).strip().upper()
    if units == "fl":
        s = s.replace("FL","").strip()
        n = _to_float(s)
        return 0.0 if n is None else n * 100.0 * 0.3048
    n = _to_float(s.replace("FT","").strip())
    if n is None:
        return 0.0
    if units == "ft":
        return n * 0.3048
    return n

def build_kml(points_rows, *, points_alt_mode="absolute", extrude_points=False,
              route_rows=None, route_alt_mode="absolute", route_color="#00A0FF",
              route_width=3.0, extrude_route=False):
    """
    points_rows: lista de dicts {name, lat, lon, alt_m, extra(dict)}
    route_rows:  lista de dicts {name, lat, lon, alt_m}
    """
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<kml xmlns="http://www.opengis.net/kml/2.2">')
    parts.append("<Document>")
    parts.append("<name>Aerovía</name>")

    # estilos
    parts.append(
        '<Style id="ptDefault"><IconStyle>'
        '<color>ff0000ff</color><scale>1.1</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/paddle/wht-blank.png</href></Icon>'
        '</IconStyle><LabelStyle><scale>0.9</scale></LabelStyle></Style>'
    )
    color_kml = kml_color_from_hex(route_color, alpha="ff")
    parts.append(f'<Style id="routeStyle"><LineStyle><color>{color_kml}</color><width>{route_width}</width></LineStyle></Style>')

    # puntos
    if points_rows:
        for r in points_rows:
            name = r.get("name") or "WPT"
            lat, lon, altm = r["lat"], r["lon"], float(r.get("alt_m", 0.0))
            extrude_tag = "<extrude>1</extrude>" if extrude_points else ""
            # descripción con metadata
            desc_items = []
            for k, v in (r.get("extra") or {}).items():
                desc_items.append(f"<tr><th style='text-align:left;padding-right:8px'>{k}</th><td>{v}</td></tr>")
            desc = "<![CDATA[<table>{}</table>]]>".format("".join(desc_items)) if desc_items else ""

            parts.append(
                "<Placemark>"
                f"<name>{name}</name>"
                "<styleUrl>#ptDefault</styleUrl>"
                f"<description>{desc}</description>"
                "<Point>"
                f"<altitudeMode>{points_alt_mode}</altitudeMode>"
                f"{extrude_tag}"
                f"<coordinates>{lon:.8f},{lat:.8f},{altm:.2f}</coordinates>"
                "</Point>"
                "</Placemark>"
            )

    # ruta
    if route_rows and len(route_rows) >= 2:
        coords = [f"{r['lon']:.8f},{r['lat']:.8f},{float(r['alt_m']):.2f}" for r in route_rows]
        extrude_tag = "<extrude>1</extrude>" if extrude_route else ""
        parts.append(
            "<Placemark>"
            "<name>Ruta</name>"
            "<styleUrl>#routeStyle</styleUrl>"
            "<LineString>"
            f"<altitudeMode>{route_alt_mode}</altitudeMode>"
            f"{extrude_tag}"
            "<coordinates>" + " ".join(coords) + "</coordinates>"
            "</LineString>"
            "</Placemark>"
        )

    parts.append("</Document>")
    parts.append("</kml>")
    return "\n".join(parts)

def csv_to_kml_from_files(points_csv=None, routes_csv=None, output="salida.kml"):
    """Genera un KML tomando CSV de puntos y rutas directamente desde disco."""
    points_rows = []
    if points_csv and os.path.isfile(points_csv):
        pdf = pd.read_csv(points_csv)
        lat_c, lon_c, combo_c, name_c, alt_c, type_c = detect_coord_columns(pdf)
        for _, row in pdf.iterrows():
            lat = lon = None
            if lat_c and lon_c:
                lat = to_decimal(row[lat_c], "lat")
                lon = to_decimal(row[lon_c], "lon")
            elif combo_c:
                parsed = to_decimal(row[combo_c], "lat")
                if isinstance(parsed, tuple) and parsed[0] == "PAIR":
                    lat, lon = parsed[1], parsed[2]
            if lat is None or lon is None:
                continue
            name = str(row[name_c]) if name_c else "WPT"
            alt_m = _parse_alt_value(row[alt_c], "ft") if alt_c else 0.0
            extra = {}
            if type_c:
                extra[type_c] = row[type_c]
            points_rows.append({"name": name, "lat": lat, "lon": lon, "alt_m": alt_m, "extra": extra})

    route_rows = []
    if routes_csv and os.path.isfile(routes_csv):
        rdf = pd.read_csv(routes_csv)
        for _, row in rdf.iterrows():
            lat = to_decimal(row.get("Lat"), "lat")
            lon = to_decimal(row.get("Lon"), "lon")
            if lat is None or lon is None:
                continue
            alt_m = _parse_alt_value(row.get("Alt"), "ft")
            route_rows.append({"name": row.get("Name", "WPT"), "lat": lat, "lon": lon, "alt_m": alt_m})

    kml = build_kml(points_rows, points_alt_mode="absolute", extrude_points=False,
                    route_rows=route_rows, route_alt_mode="absolute", route_color="#00A0FF",
                    route_width=3.0, extrude_route=False)
    with open(output, "w", encoding="utf-8") as f:
        f.write(kml)
    return output

def main_cli():
    parser = argparse.ArgumentParser(description="Genera KML desde CSV sin Streamlit")
    parser.add_argument("--points", help="CSV con waypoints", default=None)
    parser.add_argument("--routes", help="CSV con rutas", default=None)
    parser.add_argument("--output", help="Archivo KML de salida", default="salida.kml")
    args = parser.parse_args()
    out = csv_to_kml_from_files(args.points, args.routes, args.output)
    print(f"KML guardado en {out}")

# =========================
# UI (Streamlit)
# =========================

def run_streamlit_app():
    st.set_page_config(page_title="Airway Builder", layout="wide")
    st.title("🛫 Airway Builder (KML)")

    with st.sidebar:
        st.header("Opciones generales")
        pt_alt_mode = st.selectbox("Altitud puntos", ["absolute", "relativeToGround", "clampToGround"], index=0)
        rt_alt_mode = st.selectbox("Altitud ruta",   ["absolute", "relativeToGround", "clampToGround"], index=0)
        rt_color = st.color_picker("Color de ruta", value="#00A0FF")
        rt_width = st.number_input("Ancho de ruta", min_value=1.0, max_value=10.0, value=3.0, step=0.5)
        extrude_pts = st.checkbox("Extrude puntos (línea al suelo)", value=False)
        extrude_route = st.checkbox("Extrude ruta (pared al suelo)", value=False)

    st.subheader("1) (Opcional) Cargar CSV de waypoints")
    data_dir = st.text_input("Carpeta de datos", value=".")
    files_in_dir = [f for f in os.listdir(data_dir) if f.lower().endswith(".csv")] if os.path.isdir(data_dir) else []
    selected_wp = st.selectbox("Seleccionar CSV del directorio", ["(ninguno)"] + files_in_dir)

    wp_df = None
    wp_name_col = None
    wp_lat_col = None
    wp_lon_col = None
    wp_combo_col = None

    if selected_wp != "(ninguno)":
        wp_df = pd.read_csv(os.path.join(data_dir, selected_wp))
    else:
        waypoints_file = st.file_uploader("CSV con waypoints (columnas: nombre opcional, lat/lon o coordenada combinada)", type=["csv"])
        if waypoints_file:
            try:
                wp_df = pd.read_csv(waypoints_file)
            except Exception:
                waypoints_file.seek(0)
                wp_df = pd.read_csv(waypoints_file, sep=";")

    if wp_df is not None:
        st.dataframe(wp_df.head(20), use_container_width=True)
        cols = ["(ninguna)"] + list(wp_df.columns)
        wp_name_col = st.selectbox("Columna nombre (opcional)", cols, index=0)
        lat_candidates = [c for c in wp_df.columns if re.search(r"lat", c, flags=re.I)]
        lon_candidates = [c for c in wp_df.columns if re.search(r"lon|long", c, flags=re.I)]
        combo_candidates = [c for c in wp_df.columns if re.search(r"coord|wgs|geog|position|pos|location", c, flags=re.I)]

        wp_lat_col = st.selectbox("Columna Lat (si existe)", ["(ninguna)"] + lat_candidates, index=0)
        wp_lon_col = st.selectbox("Columna Lon (si existe)", ["(ninguna)"] + lon_candidates, index=0)
        wp_combo_col = st.selectbox("Columna coordenada combinada (si existe)", ["(ninguna)"] + combo_candidates, index=0)

    # Estado de la ruta
    if "route_rows" not in st.session_state:
        st.session_state.route_rows = []

    # Maestro de aerovías en sesión
    if "master_df" not in st.session_state:
        st.session_state.master_df = pd.DataFrame(columns=["Aerovia","Sec","Name","Lat","Lon","Alt"])

    st.subheader("2) Construir ruta — puntos y altitudes")
    c1, c2 = st.columns(2)

    with c1:
        st.markdown("**Agregar punto manualmente**")
        name_in = st.text_input("Nombre/ID del punto", value="")
        coord_in = st.text_input("Coordenada (lat,lon o DMS/compacto por separado)", value="")
        lat_in = st.text_input("Lat (si no usas 'lat,lon')", value="")
        lon_in = st.text_input("Lon (si no usas 'lat,lon')", value="")
        alt_val = st.text_input("Altitud del punto (ej 7500, 2300 ft, FL120)", value="")
        alt_units = st.selectbox("Unidades alt", ["ft", "m", "fl"], index=0)
        if st.button("➕ Agregar punto manual"):
            lat = None; lon = None
            if coord_in.strip():
                parsed = to_decimal(coord_in, "lat")
                if isinstance(parsed, tuple) and parsed and parsed[0] == "PAIR":
                    lat, lon = parsed[1], parsed[2]
                else:
                    st.warning("Usaste un solo valor en 'Coordenada'. Escribe 'lat,lon' o usa los campos Lat/Lon.")
            if lat is None or lon is None:
                lat = to_decimal(lat_in, "lat")
                lon = to_decimal(lon_in, "lon")
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                st.error("Coordenadas inválidas.")
            else:
                alt_m = alt_to_meters(alt_val, alt_units)
                st.session_state.route_rows.append({"name": name_in or "WPT", "lat": lat, "lon": lon, "alt_m": alt_m})

    with c2:
        st.markdown("**Agregar desde CSV de waypoints**")
        if wp_df is not None:
            # selector de fila
            idx = st.number_input("Fila a agregar (0 = primera visible)", min_value=0, max_value=len(wp_df)-1, value=0, step=1)
            def get_latlon_from_wp(row):
                lat = lon = None
                if wp_lat_col and wp_lat_col != "(ninguna)" and wp_lon_col and wp_lon_col != "(ninguna)":
                    lat = to_decimal(row[wp_lat_col], "lat"); lon = to_decimal(row[wp_lon_col], "lon")
                elif wp_combo_col and wp_combo_col != "(ninguna)":
                    parsed = to_decimal(row[wp_combo_col], "lat")
                    if isinstance(parsed, tuple) and parsed[0] == "PAIR":
                        lat, lon = parsed[1], parsed[2]
                return lat, lon

            default_alt = st.text_input("Altitud por defecto (p.ej. 7500 ft)", value="7500 ft")
            default_units = st.selectbox("Unidades por defecto", ["ft","m","fl"], index=0)
            if st.button("➕ Agregar punto desde CSV"):
                row = wp_df.iloc[int(idx)]
                lat, lon = get_latlon_from_wp(row)
                if lat is None or lon is None:
                    st.error("No se pudo leer lat/lon de esa fila (revisa columnas).")
                else:
                    name = str(row[wp_name_col]) if (wp_name_col and wp_name_col != "(ninguna)") else "WPT"
                    alt_m = alt_to_meters(default_alt, default_units)
                    st.session_state.route_rows.append({"name": name, "lat": lat, "lon": lon, "alt_m": alt_m})

    # Editor de la ruta (reordenable)
    st.markdown("**Ruta (ordena filas para definir inicio→fin):**")
    route_df = pd.DataFrame(st.session_state.route_rows, columns=["name","lat","lon","alt_m"])
    edited = st.data_editor(
        route_df,
        num_rows="dynamic",
        use_container_width=True,
        key="route_editor",
    )

    # Actualizar estado con cambios del editor
    st.session_state.route_rows = edited.to_dict("records")

    st.subheader("3) Rumbo y corrección por tramo")

    use_rhumb = st.radio(
        "Modo de cálculo para corrección del punto siguiente",
        ["Loxodrómico (rumbo constante)", "Geodésico (gran círculo)"],
        index=0, horizontal=True
    )

    legs_rows = []
    rows = st.session_state.route_rows

    for i in range(len(rows)-1):
        a, b = rows[i], rows[i+1]
        brg = initial_bearing_true(a["lat"], a["lon"], b["lat"], b["lon"])
        d_gc_m = gc_distance_m(a["lat"], a["lon"], b["lat"], b["lon"])
        d_rh_m = rhumb_distance_m(a["lat"], a["lon"], b["lat"], b["lon"])
        d_nm = d_rh_m/1852.0 if use_rhumb.startswith("Loxo") else d_gc_m/1852.0

        with st.expander(f"Tramo {i+1}: {a['name']} → {b['name']}"):
            st.write(f"**Rumbo actual (°T):** {None if brg is None else round(brg,1)}")
            st.write(f"**Distancia actual:** {d_nm:.2f} NM ({'loxodrómica' if use_rhumb.startswith('Loxo') else 'gran círculo'})")

            desired = st.number_input(
                f"Rumbo deseado (°T) — Tramo {i+1}",
                min_value=0.0, max_value=360.0,
                value=45.0, step=0.1, key=f"brg_des_{i}"
            )

            keep_dist = st.checkbox(f"Mantener distancia actual ({d_nm:.2f} NM) — Tramo {i+1}", value=True, key=f"keepd_{i}")
            if keep_dist:
                dist_nm = d_nm
            else:
                dist_nm = st.number_input(f"Distancia (NM) — Tramo {i+1}", min_value=0.0, value=d_nm, step=0.1, key=f"dist_{i}")

            colA, colB = st.columns(2)
            with colA:
                if st.button(f"🔧 Corregir punto final del tramo {i+1}", key=f"fix_{i}"):
                    dist_m = dist_nm * 1852.0
                    if use_rhumb.startswith("Loxo"):
                        new_lat, new_lon = destination_rhumb(a["lat"], a["lon"], desired, dist_m)
                    else:
                        new_lat, new_lon = destination_gc(a["lat"], a["lon"], desired, dist_m)
                    # Mantener la altitud del punto final tal como estaba
                    rows[i+1]["lat"] = float(new_lat)
                    rows[i+1]["lon"] = float(new_lon)
                    st.success(f"Tramo {i+1} corregido. Nuevo punto: lat={new_lat:.6f}, lon={new_lon:.6f}")

            with colB:
                st.caption("Nota: la altitud del punto final no cambia con esta corrección.")
            
    # Exportar
    st.subheader("4) Exportar")
    colx, coly = st.columns(2)

    with colx:
        if st.button("Generar KML"):
            # puntos = igual a la ruta (también puedes duplicar como puntos)
            points_rows = [
                {"name": r["name"], "lat": r["lat"], "lon": r["lon"], "alt_m": r["alt_m"], "extra": {}}
                for r in rows
            ]
            kml = build_kml(
                points_rows,
                points_alt_mode=pt_alt_mode, extrude_points=extrude_pts,
                route_rows=rows, route_alt_mode=rt_alt_mode,
                route_color=rt_color, route_width=rt_width, extrude_route=extrude_route
            )
            st.success("KML generado.")
            st.download_button("📥 Descargar KML", kml, file_name="aerovia.kml", mime="application/vnd.google-earth.kml+xml")

    with coly:
        if st.button("Descargar CSV de la aerovía"):
            out_df = pd.DataFrame(rows)
            csv_bytes = out_df.to_csv(index=False).encode("utf-8")
            st.download_button("📥 Descargar CSV", csv_bytes, file_name="aerovia.csv", mime="text/csv")

    st.markdown("---")
    st.subheader("5) Maestro de aerovías (append en memoria)")

    colm1, colm2 = st.columns([2,1])

    with colm1:
        st.markdown("**Cargar CSV maestro (opcional)** — columnas esperadas: `Aerovia, Sec, Name (opcional), Lat, Lon, Alt`")
        files_in_dir = [f for f in os.listdir(data_dir) if f.lower().endswith(".csv")] if os.path.isdir(data_dir) else []
        selected_master = st.selectbox("Seleccionar maestro del directorio", ["(ninguno)"] + files_in_dir)
        master_file = None
        if selected_master != "(ninguno)":
            try:
                mdf = pd.read_csv(os.path.join(data_dir, selected_master))
                needed = {"Aerovia","Sec","Lat","Lon","Alt"}
                if not needed.issubset(set(mdf.columns)):
                    st.error("El CSV maestro debe contener al menos: Aerovia, Sec, Lat, Lon, Alt (Name es opcional).")
                else:
                    if "Name" not in mdf.columns:
                        mdf["Name"] = ""
                    mdf = mdf[["Aerovia","Sec","Name","Lat","Lon","Alt"]].copy()
                    mdf["Sec"] = pd.to_numeric(mdf["Sec"], errors="coerce").astype("Int64")
                    mdf["Lat"] = pd.to_numeric(mdf["Lat"], errors="coerce")
                    mdf["Lon"] = pd.to_numeric(mdf["Lon"], errors="coerce")
                    st.session_state.master_df = mdf
                    st.success("Maestro cargado en sesión.")
            except Exception as e:
                st.error(f"No se pudo leer el maestro: {e}")
        else:
            master_file = st.file_uploader("CSV maestro", type=["csv"], key="master_upl")
            if master_file is not None:
                try:
                    mdf = pd.read_csv(master_file)
                    needed = {"Aerovia","Sec","Lat","Lon","Alt"}
                    if not needed.issubset(set(mdf.columns)):
                        st.error("El CSV maestro debe contener al menos: Aerovia, Sec, Lat, Lon, Alt (Name es opcional).")
                    else:
                        if "Name" not in mdf.columns:
                            mdf["Name"] = ""
                        mdf = mdf[["Aerovia","Sec","Name","Lat","Lon","Alt"]].copy()
                        mdf["Sec"] = pd.to_numeric(mdf["Sec"], errors="coerce").astype("Int64")
                        mdf["Lat"] = pd.to_numeric(mdf["Lat"], errors="coerce")
                        mdf["Lon"] = pd.to_numeric(mdf["Lon"], errors="coerce")
                        st.session_state.master_df = mdf
                        st.success("Maestro cargado en sesión.")
                except Exception as e:
                    st.error(f"No se pudo leer el maestro: {e}")

    with colm2:
        if st.button("🧹 Vaciar maestro (sesión)"):
            st.session_state.master_df = pd.DataFrame(columns=["Aerovia","Sec","Name","Lat","Lon","Alt"])

    st.markdown("**Configurar append**")

    cxa, cxb, cxc = st.columns([2,1,1])
    with cxa:
        route_id = st.text_input("ID de aerovía para la RUTA ACTUAL (p. ej. A1)", value="A1")
    with cxb:
        export_units = st.selectbox("Unidades Alt en maestro", ["ft","m","fl"], index=0)
    with cxc:
        sec_mode = st.selectbox("Numeración Sec", ["continuar si existe", "reiniciar en 1"], index=0)

    # Calcular Sec inicial según el maestro existente
    def _next_sec_for_route(df, route_id):
        sub = df[df["Aerovia"] == route_id]
        if sub.empty:
            return 1
        mx = pd.to_numeric(sub["Sec"], errors="coerce").dropna()
        return int(mx.max()) + 1 if len(mx) else 1

    rows = st.session_state.route_rows  # puntos de la ruta actual (en el editor)
    if st.button("➕ Agregar RUTA ACTUAL al maestro"):
        if not rows or len(rows) < 2:
            st.warning("Agrega al menos 2 puntos a la ruta antes de añadir al maestro.")
        else:
            # Determinar Sec inicial
            if sec_mode == "reiniciar en 1":
                sec_start = 1
            else:
                sec_start = _next_sec_for_route(st.session_state.master_df, route_id)

            # Construir DF de la ruta actual normalizado
            cur = pd.DataFrame(rows)
            cur = cur[["name","lat","lon","alt_m"]].copy()
            cur["Aerovia"] = route_id
            cur["Sec"] = list(range(sec_start, sec_start + len(cur)))
            cur["Name"] = cur["name"].fillna("WPT")
            cur["Lat"] = cur["lat"].astype(float)
            cur["Lon"] = cur["lon"].astype(float)
            # Convertir altitud a unidades de exportación
            cur["Alt"] = [meters_to_units(m, export_units) for m in cur["alt_m"]]
            cur = cur[["Aerovia","Sec","Name","Lat","Lon","Alt"]]

            # Append al maestro en sesión
            st.session_state.master_df = pd.concat([st.session_state.master_df, cur], ignore_index=True)
            st.success(f"Ruta '{route_id}' añadida al maestro ({len(cur)} puntos).")

    st.markdown("**Vista previa del maestro (últimas 200 filas):**")
    st.dataframe(st.session_state.master_df.tail(200), use_container_width=True)

    # Descargar / Guardar
    colsave1, colsave2 = st.columns([1,1])
    with colsave1:
        if not st.session_state.master_df.empty:
            csv_bytes = st.session_state.master_df.to_csv(index=False).encode("utf-8")
            st.download_button("📥 Descargar maestro actualizado", csv_bytes, file_name="aerovias_maestro.csv", mime="text/csv")

    with colsave2:
        save_path = st.text_input("Guardar en disco (ruta local, opcional)", value="")
        if save_path and st.button("💾 Guardar maestro en disco (sobrescribe)"):
            try:
                st.session_state.master_df.to_csv(save_path, index=False, encoding="utf-8")
                st.success(f"Guardado en: {save_path}")
            except Exception as e:
                st.error(f"No se pudo guardar: {e}")

if __name__ == "__main__":
    if st._is_running_with_streamlit:
        run_streamlit_app()
    else:
        main_cli()

