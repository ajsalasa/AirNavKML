#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Airway Builder — Crea aerovías interactivamente y exporta a KML
Requisitos:  pip install streamlit pandas

Ejecuta:  streamlit run airway_builder.py
"""

import math
import re
import os
import pandas as pd
import streamlit as st

# Rutas de datos predefinidas
WAYPOINTS_FILE = "ENR4_4_CR_2025-05-07.csv"
MAESTRO_FILE = "aerovias_maestro.csv"

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


def build_kml_multi(points_lists, routes_dict, *, points_alt_mode="absolute", extrude_points=False,
                    route_alt_mode="absolute", route_color="#00A0FF", route_width=3.0,
                    extrude_route=False):
    """Version extendida que acepta múltiples listas de puntos y rutas."""
    points_rows = []
    for lst in points_lists or []:
        points_rows.extend(lst)

    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<kml xmlns="http://www.opengis.net/kml/2.2">')
    parts.append("<Document>")
    parts.append("<name>Aerovía</name>")

    parts.append(
        '<Style id="ptDefault"><IconStyle>'
        '<color>ff0000ff</color><scale>1.1</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/paddle/wht-blank.png</href></Icon>'
        '</IconStyle><LabelStyle><scale>0.9</scale></LabelStyle></Style>'
    )
    color_kml = kml_color_from_hex(route_color, alpha="ff")
    parts.append(f'<Style id="routeStyle"><LineStyle><color>{color_kml}</color><width>{route_width}</width></LineStyle></Style>')

    if points_rows:
        for r in points_rows:
            name = r.get("name") or "WPT"
            lat, lon, altm = r["lat"], r["lon"], float(r.get("alt_m", 0.0))
            extrude_tag = "<extrude>1</extrude>" if extrude_points else ""
            desc_items = []
            for k, v in (r.get("extra") or {}).items():
                desc_items.append(f"<tr><th style='text-align:left;padding-right:8px'>{k}</th><td>{v}</td></tr>")
            desc = "<![CDATA[<table>{}</table>]]>".format("".join(desc_items)) if desc_items else ""
            parts.append(
                "<Placemark>",
                f"<name>{name}</name>",
                "<styleUrl>#ptDefault</styleUrl>",
                f"<description>{desc}</description>",
                "<Point>",
                f"<altitudeMode>{points_alt_mode}</altitudeMode>",
                f"{extrude_tag}",
                f"<coordinates>{lon:.8f},{lat:.8f},{altm:.2f}</coordinates>",
                "</Point>",
                "</Placemark>",
            )

    if routes_dict:
        for rname, rows in routes_dict.items():
            if len(rows) < 2:
                continue
            coords = [f"{r['lon']:.8f},{r['lat']:.8f},{float(r.get('alt_m',0.0)):.2f}" for r in rows]
            extrude_tag = "<extrude>1</extrude>" if extrude_route else ""
            parts.append(
                "<Placemark>",
                f"<name>{rname}</name>",
                "<styleUrl>#routeStyle</styleUrl>",
                "<LineString>",
                f"<altitudeMode>{route_alt_mode}</altitudeMode>",
                f"{extrude_tag}",
                "<coordinates>" + " ".join(coords) + "</coordinates>",
                "</LineString>",
                "</Placemark>",
            )

    parts.append("</Document>")
    parts.append("</kml>")
    return "\n".join(parts)


def google_maps_preview_html(route_rows, api_key):
    """Genera HTML para previsualizar la ruta en Google Maps.

    ``route_rows`` debe ser una lista de dicts con ``lat`` y ``lon``.
    Se necesita una clave de API válida de Google Maps.
    """

    if not route_rows:
        return ""

    center_lat = sum(r["lat"] for r in route_rows) / len(route_rows)
    center_lon = sum(r["lon"] for r in route_rows) / len(route_rows)
    coords_js = ",".join(
        f"{{lat:{r['lat']:.6f}, lng:{r['lon']:.6f}}}" for r in route_rows
    )

    html = f"""
    <div id="map" style="height:400px;"></div>
    <script src="https://maps.googleapis.com/maps/api/js?key={api_key}"></script>
    <script>
      const coords = [{coords_js}];
      const map = new google.maps.Map(document.getElementById('map'), {{
        zoom: 6,
        center: {{lat: {center_lat:.6f}, lng: {center_lon:.6f}}},
        mapTypeId: 'terrain'
      }});
      const route = new google.maps.Polyline({{
        path: coords,
        geodesic: true,
        strokeColor: '#FF0000',
        strokeOpacity: 1.0,
        strokeWeight: 2
      }});
      route.setMap(map);
      coords.forEach(p => new google.maps.Marker({{position: p, map: map}}));
    </script>
    """
    return html


def load_enr_csv(path):
    """Carga ENR4_4_CR_2025-05-07.csv como lista de dicts."""
    if not os.path.isfile(path):
        return []
    try:
        df = pd.read_csv(path, sep=";")
    except Exception:
        return []
    rows = []
    for _, r in df.iterrows():
        lat = _to_float(r.get("lat"))
        lon = _to_float(r.get("lon"))
        if lat is None or lon is None:
            continue
        name = str(r.get("designador", "WPT"))
        rows.append({"name": name, "lat": float(lat), "lon": float(lon), "alt_m": 0.0})
    return rows


def load_maestro_csv(path):
    """Carga aerovias_maestro.csv agrupado por Aerovia."""
    if not os.path.isfile(path):
        return {}
    try:
        df = pd.read_csv(path)
    except Exception:
        return {}
    routes = {}
    for aerovia, grp in df.groupby("Aerovia"):
        grp = grp.sort_values("Sec")
        lst = []
        for _, r in grp.iterrows():
            lat = _to_float(r.get("Lat"))
            lon = _to_float(r.get("Lon"))
            if lat is None or lon is None:
                continue
            name = str(r.get("Name", "WPT"))
            alt_m = alt_to_meters(r.get("Alt"), "ft")
            lst.append({"name": name, "lat": float(lat), "lon": float(lon), "alt_m": alt_m})
        routes[aerovia] = lst
    return routes

# =========================
# UI (Streamlit)
# =========================


def _add_point():
    df = st.session_state.points_df
    new_row = {
        "Sec": len(df) + 1,
        "Name": "",
        "Lat": "",
        "Lon": "",
        "Alt": "",
        "Eliminar": False,
    }
    st.session_state.points_df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)


def _delete_points():
    df = st.session_state.points_df
    df = df[~df["Eliminar"]].reset_index(drop=True)
    df["Sec"] = range(1, len(df) + 1)
    st.session_state.points_df = df


def run_streamlit_app():
    st.set_page_config(page_title="Airway Builder", layout="wide")
    st.title("🛫 Airway Builder (KML)")

    if "points_df" not in st.session_state:
        st.session_state.points_df = pd.DataFrame(
            columns=["Sec", "Name", "Lat", "Lon", "Alt", "Eliminar"]
        )

    edited = st.data_editor(
        st.session_state.points_df,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Eliminar": st.column_config.CheckboxColumn(
                "Eliminar", help="Marca para borrar este punto"
            ),
        },
        key="points_editor",
    )
    st.caption("Rellena la tabla con los puntos de la aerovía en orden.")
    st.session_state.points_df = edited

    col_add, col_del, col_kml = st.columns(3)
    with col_add:
        st.button("Añadir punto", on_click=_add_point, help="Agrega una fila vacía al final.")
    with col_del:
        st.button(
            "Borrar",
            on_click=_delete_points,
            help="Elimina las filas marcadas con 'Eliminar'.",
        )
    with col_kml:
        valid_rows = []
        for _, r in st.session_state.points_df.iterrows():
            lat = _to_float(r.get("Lat"))
            lon = _to_float(r.get("Lon"))
            if lat is None or lon is None:
                continue
            alt_m = alt_to_meters(r.get("Alt"), "ft")
            name = r.get("Name") or "WPT"
            valid_rows.append(
                {"name": name, "lat": float(lat), "lon": float(lon), "alt_m": alt_m}
            )
        kml = build_kml(valid_rows) if len(valid_rows) >= 2 else ""
        st.download_button(
            "Guardar KML",
            kml,
            file_name="aerovia.kml",
            mime="application/vnd.google-earth.kml+xml",
            disabled=len(valid_rows) < 2,
            help="Descarga los puntos actuales en formato KML.",
        )

def is_running_with_streamlit() -> bool:
    """Return True when executed via ``streamlit run``.

    Older versions of this script relied on the private attribute
    ``st._is_running_with_streamlit``, which has been removed in recent
    versions of Streamlit.  This helper uses the public runtime API when
    available and gracefully falls back to ``False`` when the check cannot
    be performed.
    """

    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:  # pragma: no cover - best effort; imports may fail
        return False


if __name__ == "__main__":
    if is_running_with_streamlit():
        run_streamlit_app()
    else:
        print("Este script debe ejecutarse con 'streamlit run airway_builder.py'.")
