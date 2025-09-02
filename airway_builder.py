#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Airway KML Editor — Streamlit
- Waypoints: cargados SIEMPRE desde CSV y exportados como carpeta "Waypoints".
- Rutas: múltiples, editables (puntos desde CSV o manuales), altitud por punto, corrección de rumbo (loxodrómica).
- Vista en mapa (pydeck) de todo el proyecto.
- Un único KML del proyecto en la raíz del repo (junto al script): project.kml
- Exporta CSV maestro con todas las rutas (opcional).

Requisitos:
  pip install streamlit pandas pydeck

Ejecuta:
  streamlit run airway_editor.py
"""

import math, re, json
from pathlib import Path
from typing import Dict, List, Tuple
import os
import pandas as pd
import streamlit as st
import pydeck as pdk

# =========================
# CONFIG
# =========================
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_WP_CSV = PROJECT_DIR / "ENR4_4_CR_2025-05-07.csv"  # ajusta si tu CSV tiene otro nombre
PROJECT_KML = PROJECT_DIR / "project.kml"                   # único KML del proyecto
STATE_JSON   = PROJECT_DIR / "routes_state.json"            # persistencia ligera (opcional)

# Si tu CSV tiene nombres de columnas distintos, ajusta aquí:
WP_NAME_COL_DEFAULT = "designador"   # ejemplo de tu dataset
WP_LAT_COL_DEFAULT  = "lat"
WP_LON_COL_DEFAULT  = "lon"
WP_COMBO_COL_DEFAULT = None          # si no tienes Lat/Lon y usas columna combinada (Coord/WGS/etc.)

# =========================
# UTILIDADES PARSEO / ALT
# =========================
def _to_float(x):
    if x is None or (isinstance(x, float) and math.isnan(x)): return None
    if isinstance(x, (int, float)): return float(x)
    try:
        s = str(x).strip().replace(",", ".")
        return float(s)
    except: return None

def parse_dms_piece(piece):
    piece = re.sub(r"[NSEW]", "", str(piece), flags=re.I).strip().replace("º","°")
    piece = re.sub(r"[°]", " ", piece).replace("'", " ").replace('"', " ").replace("’"," ").replace("′"," ")
    toks = [t for t in re.split(r"[\s:]+", piece.strip()) if t]
    if len(toks)==3: deg, mi, se = float(toks[0]), float(toks[1]), float(toks[2])
    elif len(toks)==2: deg, mi, se = float(toks[0]), float(toks[1]), 0.0
    elif len(toks)==1:
        val=_to_float(toks[0]); 
        if val is None: raise ValueError("DMS inválido")
        return val
    else: raise ValueError("DMS inválido")
    return deg + mi/60.0 + se/3600.0

def parse_compact_dms(piece, guess_lon=False):
    s = str(piece).strip().upper()
    m  = re.match(r"^([0-9.]+)\s*([NSEW])$", s)
    m2 = re.match(r"^([NSEW])\s*([0-9.]+)$", s)
    hem=None; core=None
    if m: core, hem = m.group(1), m.group(2)
    elif m2: hem, core = m2.group(1), m2.group(2)
    else: core = re.sub(r"[^0-9.]", "", s)
    if not core: raise ValueError("Compact DMS inválido")
    if "." in core: left, frac = core.split(".",1); frac="."+frac
    else: left, frac = core, ""
    deg_len = 2 if len(left) in (6,7) else 3 if len(left) in (7,8,9) else (3 if guess_lon else 2)
    if len(left) < deg_len+4: left = left.rjust(deg_len+4, "0")
    deg=float(left[:deg_len]); mm=float(left[deg_len:deg_len+2]); ss=float(left[deg_len+2:deg_len+4])
    if frac: ss=float(f"{int(ss)}{frac}")
    val=deg + mm/60.0 + ss/3600.0
    if hem in ("S","W"): val=-val
    return val

def to_decimal(coord_str, context="lat"):
    if coord_str is None or (isinstance(coord_str, float) and math.isnan(coord_str)): return None
    if isinstance(coord_str,(int,float)): return float(coord_str)
    s=str(coord_str).strip().replace(",", ".")
    try:
        if not re.search(r"[NSEW]$", s, flags=re.I): return float(s)
    except: pass
    if "," in s:
        parts=[p.strip() for p in s.split(",")]
        if len(parts)==2 and re.search(r"\d",parts[0]) and re.search(r"\d",parts[1]):
            la=to_decimal(parts[0],"lat"); lo=to_decimal(parts[1],"lon")
            return ("PAIR", la, lo)
    if re.search(r"[°'\"′]|:", s):
        val=parse_dms_piece(s)
        if re.search(r"[SW]", s, flags=re.I): val=-abs(val)
        if re.search(r"[NE]", s, flags=re.I): val=abs(val)
        return val
    try:
        return parse_compact_dms(s, guess_lon=(context=="lon"))
    except: pass
    s2=re.sub(r"[^0-9\.\-]","", s)
    try: return float(s2)
    except: return None

def alt_to_meters(val, units):
    if val is None: return 0.0
    s=str(val).strip().upper()
    if units=="fl":
        s=s.replace("FL","").strip(); n=_to_float(s)
        return 0.0 if n is None else n*100.0*0.3048
    if units=="ft":
        n=_to_float(s.replace("FT","").strip())
        return 0.0 if n is None else n*0.3048
    n=_to_float(s); return 0.0 if n is None else n

def meters_to_units(m, units):
    if m is None: return None
    if units=="m":  return round(float(m),2)
    if units=="ft": return round(float(m)/0.3048,1)
    if units=="fl":
        ft=float(m)/0.3048; fl=round(ft/100.0); return f"FL{int(fl):03d}"
    return m

# =========================
# NAV: rumbo y loxodromia
# =========================
EARTH_R_M = 6_371_000.0

def initial_bearing_true(lat1, lon1, lat2, lon2):
    if None in (lat1,lon1,lat2,lon2): return None
    φ1,λ1,φ2,λ2 = map(math.radians,[lat1,lon1,lat2,lon2])
    dλ = λ2-λ1
    y = math.sin(dλ)*math.cos(φ2)
    x = math.cos(φ1)*math.sin(φ2) - math.sin(φ1)*math.cos(φ2)*math.cos(dλ)
    θ = math.atan2(y,x)
    return (math.degrees(θ)+360.0)%360.0

def _norm_lon(lon_deg): return (lon_deg+180.0)%360.0-180.0

def rhumb_distance_m(lat1,lon1,lat2,lon2):
    φ1,λ1,φ2,λ2 = map(math.radians,[lat1,lon1,lat2,lon2])
    dφ, dλ = φ2-φ1, λ2-λ1
    if abs(dλ)>math.pi: dλ -= math.copysign(2*math.pi,dλ)
    Δψ = math.log(math.tan(math.pi/4+φ2/2)/math.tan(math.pi/4+φ1/2)) if φ2!=φ1 else 0.0
    q = dφ/Δψ if abs(Δψ)>1e-12 else math.cos(φ1)
    δ = math.sqrt(dφ*dφ + (q*dλ)*(q*dλ))
    return δ*EARTH_R_M

def destination_rhumb(lat1,lon1,bearing_deg,distance_m):
    θ = math.radians(bearing_deg)
    φ1,λ1 = math.radians(lat1), math.radians(lon1)
    δ = distance_m/EARTH_R_M
    dφ = δ*math.cos(θ)
    φ2 = φ1 + dφ
    if abs(φ2)>math.pi/2: φ2 = math.copysign(math.pi/2-1e-12, φ2)
    Δψ = math.log(math.tan(math.pi/4+φ2/2)/math.tan(math.pi/4+φ1/2)) if φ2!=φ1 else 0.0
    q  = dφ/Δψ if abs(Δψ)>1e-12 else math.cos(φ1)
    dλ = δ*math.sin(θ)/q
    λ2 = λ1+dλ
    return math.degrees(φ2), _norm_lon(math.degrees(λ2))

# =========================
# KML helpers
# =========================
def kml_color_from_hex(hex_str, alpha="ff"):
    s=str(hex_str).strip().lstrip("#")
    if len(s)==8: return s.lower()      # ya AABBGGRR
    if len(s)!=6: return "ff0000ff"     # rojo
    rr,gg,bb = s[0:2], s[2:4], s[4:6]
    return (alpha+bb+gg+rr).lower()

def build_kml_project(
    wp_df: pd.DataFrame,
    wp_name_col: str,
    wp_lat_col: str,
    wp_lon_col: str,
    wp_combo_col: str|None,
    *,
    waypoint_alt_mode: str = "clampToGround",
    routes: Dict[str, dict] = None
)->str:
    """
    routes[route_id] = {
        'color': '#RRGGBB', 'width': float, 'alt_mode': 'absolute|relativeToGround|clampToGround',
        'extrude': bool, 'start_end_icons': bool,
        'points': [{name, lat, lon, alt_m}, ...]
    }
    """
    routes = routes or {}
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<kml xmlns="http://www.opengis.net/kml/2.2">')
    parts.append("<Document>")
    parts.append("<name>Project</name>")

    # Estilos genéricos puntos
    parts.append(
        '<Style id="ptDefault"><IconStyle>'
        '<color>ff0000ff</color><scale>1.1</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/paddle/wht-blank.png</href></Icon>'
        '</IconStyle><LabelStyle><scale>0.9</scale></LabelStyle></Style>'
    )
    parts.append(
        '<Style id="start"><IconStyle>'
        '<color>ff00ff00</color><scale>1.2</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/paddle/grn-circle.png</href></Icon>'
        '</IconStyle><LabelStyle><scale>1.0</scale></LabelStyle></Style>'
    )
    parts.append(
        '<Style id="end"><IconStyle>'
        '<color>ff0000ff</color><scale>1.2</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/paddle/red-circle.png</href></Icon>'
        '</IconStyle><LabelStyle><scale>1.0</scale></LabelStyle></Style>'
    )

    # ---- Folder: Waypoints
    parts.append("<Folder><name>Waypoints</name>")
    # Detectar coordenadas:
    def _row_to_latlon(row)->Tuple[float|None,float|None]:
        if wp_lat_col and wp_lon_col and (wp_lat_col in row) and (wp_lon_col in row):
            lat=to_decimal(row[wp_lat_col],"lat"); lon=to_decimal(row[wp_lon_col],"lon")
            return lat, lon
        if wp_combo_col and (wp_combo_col in row):
            parsed = to_decimal(row[wp_combo_col], "lat")
            if isinstance(parsed, tuple) and parsed[0]=="PAIR": return parsed[1], parsed[2]
        return None, None

    for _,row in wp_df.iterrows():
        lat, lon = _row_to_latlon(row)
        if lat is None or lon is None: continue
        name = str(row.get(wp_name_col, "WPT"))
        parts.append(
            "<Placemark>"
            f"<name>{name}</name>"
            "<styleUrl>#ptDefault</styleUrl>"
            "<Point>"
            f"<altitudeMode>{waypoint_alt_mode}</altitudeMode>"
            f"<coordinates>{lon:.8f},{lat:.8f},0</coordinates>"
            "</Point>"
            "</Placemark>"
        )
    parts.append("</Folder>")  # /Waypoints

    # ---- Folder: Routes
    parts.append("<Folder><name>Routes</name>")
    for rid, cfg in routes.items():
        color_hex = cfg.get("color","#00A0FF")
        width     = float(cfg.get("width",3.0))
        alt_mode  = cfg.get("alt_mode","absolute")
        extrude   = bool(cfg.get("extrude", False))
        se_icons  = bool(cfg.get("start_end_icons", True))
        pts       = cfg.get("points", [])
        kml_color = kml_color_from_hex(color_hex, "ff")

        # estilo específico por ruta
        parts.append(
            f'<Style id="route_{rid}"><LineStyle><color>{kml_color}</color><width>{width}</width></LineStyle></Style>'
        )

        # subcarpeta con puntos (opcional, útil para inspección)
        parts.append(f"<Folder><name>{rid} — Points</name>")
        for idx, p in enumerate(pts):
            name = p.get("name") or f"{rid}-{idx+1}"
            lat,lon,altm = p["lat"],p["lon"],float(p.get("alt_m",0.0))
            style_ref="#ptDefault"
            if se_icons and idx==0: style_ref="#start"
            if se_icons and idx==len(pts)-1: style_ref="#end"
            parts.append(
                "<Placemark>"
                f"<name>{name}</name>"
                f"<styleUrl>{style_ref}</styleUrl>"
                "<Point>"
                f"<altitudeMode>{alt_mode}</altitudeMode>"
                f"<coordinates>{lon:.8f},{lat:.8f},{altm:.2f}</coordinates>"
                "</Point>"
                "</Placemark>"
            )
        parts.append("</Folder>")

        # línea de ruta
        if len(pts)>=2:
            coord_str = " ".join([f"{p['lon']:.8f},{p['lat']:.8f},{float(p.get('alt_m',0.0)):.2f}" for p in pts])
            extrude_tag = "<extrude>1</extrude>" if extrude else ""
            parts.append(
                "<Placemark>"
                f"<name>{rid}</name>"
                f"<styleUrl>#route_{rid}</styleUrl>"
                "<LineString>"
                f"<altitudeMode>{alt_mode}</altitudeMode>"
                f"{extrude_tag}"
                f"<coordinates>{coord_str}</coordinates>"
                "</LineString>"
                "</Placemark>"
            )
    parts.append("</Folder>")  # /Routes

    parts.append("</Document></kml>")
    return "\n".join(parts)

def google_maps_project_preview_html(
    routes: dict,
    wp_df=None,
    wp_name_col: str = None,
    wp_lat_col: str = None,
    wp_lon_col: str = None,
    wp_combo_col: str = None,
    show_waypoints: bool = True,
    map_type: str = "terrain"  # "roadmap" | "satellite" | "hybrid" | "terrain"
):
    """
    Genera HTML para previsualizar TODO el proyecto en Google Maps:
      - Todas las rutas (polylines con color/ancho por ruta)
      - Opcional: todos los waypoints del CSV como marcadores

    routes: dict como en tu editor: { rid: {color, width, points:[{lat,lon,alt_m,name}, ...]} }
    wp_df y *_col: DataFrame y mapeo de columnas para waypoints del CSV.
    """

    # --- Serializar rutas a JSON para JS ---
    routes_js = []
    for rid, cfg in (routes or {}).items():
        pts = cfg.get("points", []) or []
        if len(pts) == 0:
            continue
        path = [{"lat": float(p["lat"]), "lng": float(p["lon"])} for p in pts if p.get("lat") is not None and p.get("lon") is not None]
        if not path:
            continue
        routes_js.append({
            "name": str(rid),
            "color": cfg.get("color", "#00A0FF"),
            "width": float(cfg.get("width", 3.0)),
            "path": path
        })

    # --- Serializar waypoints del CSV (si se pide) ---
    wps_js = []
    if show_waypoints and (wp_df is not None) and (len(wp_df) > 0):
        def _wp_latlon(row):
            lat = lon = None
            if wp_lat_col and wp_lon_col and (wp_lat_col in row) and (wp_lon_col in row):
                lat = to_decimal(row[wp_lat_col], "lat")
                lon = to_decimal(row[wp_lon_col], "lon")
            elif wp_combo_col and (wp_combo_col in row):
                pr = to_decimal(row[wp_combo_col], "lat")
                if isinstance(pr, tuple) and pr[0] == "PAIR":
                    lat, lon = pr[1], pr[2]
            return lat, lon

        for _, row in wp_df.iterrows():
            lat, lon = _wp_latlon(row)
            if lat is None or lon is None:
                continue
            wps_js.append({
                "name": str(row.get(wp_name_col, "WPT")),
                "lat": float(lat),
                "lng": float(lon)
            })

    # Centro por defecto (si no hay nada que mostrar)
    center_lat, center_lng, zoom = 10.0, -84.0, 6

    html = f"""
<div id="map" style="height:600px;border-radius:12px;"></div>
<script src="https://maps.googleapis.com/maps/api/js?key={{API_KEY}}"></script>
<script>
(function() {{
  const map = new google.maps.Map(document.getElementById('map'), {{
    center: {{lat: {center_lat:.6f}, lng: {center_lng:.6f}}},
    zoom: {zoom},
    mapTypeId: '{map_type}'
  }});

  const bounds = new google.maps.LatLngBounds();
  let havePoints = false;

  // Waypoints (opcional)
  const wps = {json.dumps(wps_js)};
  wps.forEach(w => {{
    const pos = new google.maps.LatLng(w.lat, w.lng);
    new google.maps.Marker({{ position: pos, map: map, title: w.name }});
    bounds.extend(pos); havePoints = true;
  }});

  // Rutas
  const routes = {json.dumps(routes_js)};
  routes.forEach(r => {{
    if (!r.path || r.path.length < 2) return;
    const poly = new google.maps.Polyline({{
      map: map,
      path: r.path,
      geodesic: true,
      strokeColor: r.color || '#00A0FF',
      strokeOpacity: 0.95,
      strokeWeight: Math.max(1, Math.round(r.width || 3))
    }});
    // Extender bounds con toda la ruta
    r.path.forEach(p => {{ bounds.extend(new google.maps.LatLng(p.lat, p.lng)); havePoints = true; }});
  }});

  if (havePoints) {{
    map.fitBounds(bounds);
    // Ajuste suave: si el zoom queda demasiado cerca, limítalo un poco
    const listener = google.maps.event.addListenerOnce(map, "idle", function() {{
      if (map.getZoom() > 14) map.setZoom(14);
    }});
  }}
}})();
</script>
""".replace("{API_KEY}", st.secrets.get("GOOGLE_MAPS_API_KEY", os.getenv("GOOGLE_MAPS_API_KEY", "")))
    return html

# =========================
# STREAMLIT UI
# =========================
st.set_page_config(page_title="Airway KML Editor", layout="wide")
st.title("🗺️ Airway KML Editor — proyecto único")

# Estado
if "routes" not in st.session_state:
    st.session_state.routes = {}  # rid -> cfg
if "active_route" not in st.session_state:
    st.session_state.active_route = None
if "auto_save" not in st.session_state:
    st.session_state.auto_save = False

# Sidebar — opciones globales
with st.sidebar:
    st.header("Proyecto")
    st.write(f"KML del proyecto: `{PROJECT_KML.name}`")
    st.checkbox("Autoguardar KML ante cambios", value=st.session_state.auto_save, key="auto_save")

    # Cargar / reemplazar CSV de waypoints
    st.subheader("Waypoints CSV")
    wp_file = st.file_uploader("Sube CSV (opcional, reemplaza el default)", type=["csv"])
    st.caption("Si no subes, se usará el CSV por defecto en la carpeta del proyecto.")
    st.write("Columnas del CSV (ajustables abajo):")

    with st.expander("Mapeo de columnas"):
        WP_NAME_COL = st.text_input("Columna Name", WP_NAME_COL_DEFAULT or "")
        WP_LAT_COL  = st.text_input("Columna Lat",  WP_LAT_COL_DEFAULT or "")
        WP_LON_COL  = st.text_input("Columna Lon",  WP_LON_COL_DEFAULT or "")
        WP_COMBO_COL = st.text_input("Columna combinada (Coord/WGS/Geog...)", WP_COMBO_COL_DEFAULT or "")

    # Gestor de rutas
    st.subheader("Rutas del proyecto")
    # Crear
    new_rid = st.text_input("Nueva ruta: ID", value="")
    colA,colB = st.columns([1,1])
    with colA:
        if st.button("➕ Crear ruta"):
            rid=new_rid.strip() or None
            if not rid: st.warning("Escribe un ID para la ruta."); 
            elif rid in st.session_state.routes: st.error("Ya existe una ruta con ese ID.")
            else:
                st.session_state.routes[rid] = {
                    "color": "#00A0FF", "width": 3.0, "alt_mode":"absolute",
                    "extrude": False, "start_end_icons": True, "points":[]
                }
                st.session_state.active_route = rid
    with colB:
        rids = list(st.session_state.routes.keys())
        active = st.selectbox("Ruta activa", ["(ninguna)"]+rids, index=(0 if not st.session_state.active_route else rids.index(st.session_state.active_route)+1))
        st.session_state.active_route = None if active=="(ninguna)" else active

    if st.session_state.active_route:
        rid = st.session_state.active_route
        cfg = st.session_state.routes[rid]
        st.markdown(f"**Editando:** `{rid}`")
        c1,c2,c3,c4 = st.columns([1,1,1,1])
        with c1:
            new_name = st.text_input("Renombrar ruta a:", value=rid, key=f"rename_{rid}")
            if new_name and new_name != rid and st.button("Renombrar"):
                if new_name in st.session_state.routes:
                    st.error("Ya existe una ruta con ese ID.")
                else:
                    st.session_state.routes[new_name]=st.session_state.routes.pop(rid)
                    st.session_state.active_route=new_name
                    st.success("Renombrada.")
        with c2:
            if st.button("🗑️ Eliminar ruta"):
                st.session_state.routes.pop(rid,None)
                st.session_state.active_route=None
                st.success("Ruta eliminada.")
        with c3:
            cfg["color"] = st.color_picker("Color", value=cfg.get("color","#00A0FF"), key=f"col_{rid}")
        with c4:
            cfg["width"] = st.number_input("Ancho línea (px)", min_value=1.0, max_value=12.0, value=float(cfg.get("width",3.0)), step=0.5, key=f"w_{rid}")

        c5,c6,c7 = st.columns([1,1,1])
        with c5:
            cfg["alt_mode"] = st.selectbox("Alt mode ruta", ["absolute","relativeToGround","clampToGround"], index=["absolute","relativeToGround","clampToGround"].index(cfg.get("alt_mode","absolute")), key=f"am_{rid}")
        with c6:
            cfg["extrude"] = st.checkbox("Extrude ruta", value=bool(cfg.get("extrude",False)), key=f"ex_{rid}")
        with c7:
            cfg["start_end_icons"] = st.checkbox("Start/End icons", value=bool(cfg.get("start_end_icons",True)), key=f"sei_{rid}")

# Cargar waypoints DF
def read_wp_df():
    if wp_file is not None:
        # intenta varios separadores
        for sep in [",",";","\t","|"]:
            wp_file.seek(0)
            try:
                return pd.read_csv(wp_file, sep=sep)
            except: pass
        wp_file.seek(0)
        return pd.read_csv(wp_file)
    else:
        return pd.read_csv(DEFAULT_WP_CSV, sep=";") if DEFAULT_WP_CSV.exists() else pd.DataFrame()

wp_df = read_wp_df()
st.subheader("Waypoints (del CSV)")
st.dataframe(wp_df.head(20), use_container_width=True)

# =========================
# EDITOR DE LA RUTA ACTIVA
# =========================
st.subheader("Editor de rutas")
if not st.session_state.active_route:
    st.info("Crea o selecciona una ruta en la barra lateral.")
else:
    rid = st.session_state.active_route
    cfg = st.session_state.routes[rid]

    c1,c2 = st.columns(2)
    with c1:
        st.markdown("**Agregar punto manual**")
        name_in = st.text_input("Nombre/ID del punto", value="", key=f"nm_{rid}")
        coord_in= st.text_input("Coordenada (lat,lon) o deja vacío y usa campos abajo", value="", key=f"crd_{rid}")
        lat_in  = st.text_input("Lat (si no usas 'lat,lon')", value="", key=f"lat_{rid}")
        lon_in  = st.text_input("Lon (si no usas 'lat,lon')", value="", key=f"lon_{rid}")
        alt_val = st.text_input("Altitud (ej 7500, 7500 ft, FL120)", value="", key=f"alt_{rid}")
        alt_units = st.selectbox("Unidades", ["ft","m","fl"], index=0, key=f"u_{rid}")
        if st.button("➕ Agregar punto", key=f"addm_{rid}"):
            lat=lon=None
            if coord_in.strip():
                parsed=to_decimal(coord_in,"lat")
                if isinstance(parsed,tuple) and parsed[0]=="PAIR":
                    lat,lon=parsed[1],parsed[2]
                else:
                    st.warning("Si usas 'Coordenada', escribe 'lat,lon'. O usa campos Lat/Lon.")
            if lat is None or lon is None:
                lat=to_decimal(lat_in,"lat"); lon=to_decimal(lon_in,"lon")
            if lat is None or lon is None or not (-90<=lat<=90 and -180<=lon<=180):
                st.error("Coordenadas inválidas.")
            else:
                cfg["points"].append({"name": name_in or "WPT", "lat": float(lat), "lon": float(lon), "alt_m": float(alt_to_meters(alt_val, alt_units))})
                st.success("Punto agregado.")
                if st.session_state.auto_save:  # autosave KML
                    kml = build_kml_project(wp_df, WP_NAME_COL, WP_LAT_COL, WP_LON_COL, (WP_COMBO_COL or None), routes=st.session_state.routes)
                    PROJECT_KML.write_text(kml, encoding="utf-8")

    with c2:
        st.markdown("**Agregar punto desde CSV de waypoints**")
        if not wp_df.empty:
            idx = st.number_input("Fila del CSV", min_value=0, max_value=len(wp_df)-1, value=0, step=1, key=f"ix_{rid}")
            def _wp_latlon(row):
                if WP_LAT_COL and WP_LON_COL and (WP_LAT_COL in row) and (WP_LON_COL in row):
                    return to_decimal(row[WP_LAT_COL],"lat"), to_decimal(row[WP_LON_COL],"lon")
                if WP_COMBO_COL and (WP_COMBO_COL in row):
                    pr=to_decimal(row[WP_COMBO_COL],"lat")
                    if isinstance(pr,tuple) and pr[0]=="PAIR": return pr[1],pr[2]
                return None,None
            default_alt = st.text_input("Altitud por defecto (ej 7500 ft)", value="7500 ft", key=f"da_{rid}")
            default_units = st.selectbox("Unidades por defecto", ["ft","m","fl"], index=0, key=f"du_{rid}")
            if st.button("➕ Agregar desde CSV", key=f"addcsv_{rid}"):
                row = wp_df.iloc[int(idx)]
                lat,lon=_wp_latlon(row)
                if lat is None or lon is None:
                    st.error("No se pudo leer lat/lon de esa fila.")
                else:
                    name = str(row.get(WP_NAME_COL,"WPT"))
                    cfg["points"].append({"name": name, "lat": float(lat), "lon": float(lon), "alt_m": float(alt_to_meters(default_alt, default_units))})
                    st.success("Punto agregado desde CSV.")
                    if st.session_state.auto_save:
                        kml = build_kml_project(wp_df, WP_NAME_COL, WP_LAT_COL, WP_LON_COL, (WP_COMBO_COL or None), routes=st.session_state.routes)
                        PROJECT_KML.write_text(kml, encoding="utf-8")

    # Tabla editable de la ruta
    st.markdown("**Ruta (arrástrala para ordenar inicio→fin):**")
    points_df = pd.DataFrame(cfg["points"], columns=["name","lat","lon","alt_m"])
    edited = st.data_editor(points_df, num_rows="dynamic", use_container_width=True, key=f"ed_{rid}")
    # Guardar cambios de editor en el estado
    st.session_state.routes[rid]["points"] = edited.to_dict("records")

    # Rumbo por tramo (loxodrómico)
    st.markdown("**Rumbo y corrección por tramo (Loxodrómico)**")
    rows = st.session_state.routes[rid]["points"]
    for i in range(len(rows)-1):
        a,b = rows[i], rows[i+1]
        brg  = initial_bearing_true(a["lat"],a["lon"],b["lat"],b["lon"])
        d_m  = rhumb_distance_m(a["lat"],a["lon"],b["lat"],b["lon"])
        d_nm = d_m/1852.0
        with st.expander(f"Tramo {i+1}: {a['name']} → {b['name']}"):
            st.write(f"**Rumbo actual (°T):** {None if brg is None else round(brg,1)}")
            st.write(f"**Distancia actual:** {d_nm:.2f} NM (loxodrómica)")
            desired = st.number_input(f"Rumbo deseado (°T) — Tramo {i+1}", min_value=0.0, max_value=360.0, value=45.0, step=0.1, key=f"h_{rid}_{i}")
            keep = st.checkbox(f"Mantener {d_nm:.2f} NM", value=True, key=f"keep_{rid}_{i}")
            dist_nm = d_nm if keep else st.number_input(f"Distancia (NM) — Tramo {i+1}", min_value=0.0, value=d_nm, step=0.1, key=f"dnm_{rid}_{i}")
            if st.button(f"🔧 Corregir punto final del tramo {i+1}", key=f"fix_{rid}_{i}"):
                dist_m = dist_nm*1852.0
                new_lat, new_lon = destination_rhumb(a["lat"],a["lon"], desired, dist_m)
                rows[i+1]["lat"]=float(new_lat); rows[i+1]["lon"]=float(new_lon)
                st.session_state.routes[rid]["points"]=rows
                st.success(f"Nuevo punto: lat={new_lat:.6f}, lon={new_lon:.6f}")
                if st.session_state.auto_save:
                    kml = build_kml_project(wp_df, WP_NAME_COL, WP_LAT_COL, WP_LON_COL, (WP_COMBO_COL or None), routes=st.session_state.routes)
                    PROJECT_KML.write_text(kml, encoding="utf-8")

# =========================
# MAPA — vista de TODO el proyecto
# =========================
st.markdown("**Vista previa en Google Maps (proyecto completo)**")
_gkey = st.secrets.get("GOOGLE_MAPS_API_KEY", os.getenv("GOOGLE_MAPS_API_KEY", ""))

if _gkey:
    g_html = google_maps_project_preview_html(
        routes=st.session_state.routes,     # TODAS las rutas
        wp_df=wp_df,                        # tus waypoints del CSV ya cargado
        wp_name_col=WP_NAME_COL,
        wp_lat_col=WP_LAT_COL,
        wp_lon_col=WP_LON_COL,
        wp_combo_col=(WP_COMBO_COL or None),
        show_waypoints=True,                # pon False si no quieres marcadores de WPT
        map_type="terrain"                  # "roadmap" si prefieres
    )
    st.components.v1.html(g_html, height=620)
else:
    st.info("Falta GOOGLE_MAPS_API_KEY (en secrets o variable de entorno) para ver la previsualización.")

# =========================
# EXPORTAR / GUARDAR
# =========================
st.subheader("Exportar / Guardar proyecto")
col1,col2,col3 = st.columns(3)
with col1:
    if st.button("💾 Guardar KML del proyecto"):
        kml = build_kml_project(wp_df, WP_NAME_COL, WP_LAT_COL, WP_LON_COL, (WP_COMBO_COL or None), routes=st.session_state.routes)
        PROJECT_KML.write_text(kml, encoding="utf-8")
        st.success(f"Guardado: {PROJECT_KML}")

with col2:
    # CSV maestro con TODAS las rutas (aplanado)
    if st.button("📥 Descargar CSV maestro de rutas"):
        rows=[]
        for rid,cfg in st.session_state.routes.items():
            for idx,p in enumerate(cfg.get("points",[]), start=1):
                rows.append({"Aerovia": rid, "Sec": idx, "Name": p.get("name","WPT"), "Lat": p["lat"], "Lon": p["lon"], "Alt_m": p.get("alt_m",0.0)})
        if not rows:
            st.warning("No hay rutas/puntos aún.")
        else:
            df = pd.DataFrame(rows)
            st.download_button("Descargar CSV maestro", df.to_csv(index=False).encode("utf-8"), file_name="routes_master.csv", mime="text/csv")

with col3:
    # Persistencia ligera del estado (JSON), opcional
    c1,c2 = st.columns(2)
    with c1:
        if st.button("Guardar estado (JSON)"):
            STATE_JSON.write_text(json.dumps(st.session_state.routes, indent=2), encoding="utf-8")
            st.success(f"Estado guardado: {STATE_JSON.name}")
    with c2:
        if STATE_JSON.exists() and st.button("Cargar estado (JSON)"):
            try:
                st.session_state.routes = json.loads(STATE_JSON.read_text(encoding="utf-8"))
                st.success("Estado cargado.")
            except Exception as e:
                st.error(f"No se pudo cargar: {e}")