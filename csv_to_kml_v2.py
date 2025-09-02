# =========================
# CONFIGURACIÓN
# =========================

# Entradas/Salida
POINTS_CSV      = "ENR4_4_CR_2025-05-07.csv"   # CSV con puntos (o None para no generar puntos)
ROUTES_CSV      = "aerovias_maestro.csv"# CSV con vértices de aerovías; None si no quieres rutas
KML_OUTPUT      = "salida.kml"                 # Archivo KML resultante
KML_TITLE       = "Puntos + Aerovías (demo)"

# --- Puntos (Placemark <Point>) ---
POINTS_GENERATE     = True                      # Cambia a False si NO quieres puntos
POINTS_ALT_MODE     = "relativeToGround"        # "clampToGround" | "relativeToGround" (AGL) | "absolute" (MSL)
POINTS_ALT_FIXED    = 750                        # Altitud fija (num) o None si usarás columna
POINTS_ALT_COLUMN   = None                      # p.ej. "Altura_m" si existe en el CSV
POINTS_ALT_UNITS    = "m"                       # "m" | "ft" | "fl" (FL180 = 180*100 ft)
POINTS_EXTRUDE      = True                      # Dibuja línea al suelo
POINTS_NAME_COLUMN  = None                      # Si None, se elige automáticamente
POINTS_TYPE_COLUMN  = None                      # Si None, se intenta detectar para carpetas por tipo

# --- Rutas (Placemark <LineString>) ---
ROUTES_GENERATE     = ROUTES_CSV is not None    # Se activa si hay archivo de rutas
ROUTES_GROUP_COL  = "Aerovia"
ROUTES_ORDER_COL  = "Sec"                     # p.ej. "Sec": orden de vértices en cada aerovía
ROUTES_ALT_MODE     = "absolute"        # "clampToGround" | "relativeToGround" | "absolute"
ROUTES_ALT_FIXED    = None                      # Altitud fija (num) o None si usarás columna
ROUTES_ALT_COLUMN   = "Alt"                      # p.ej. "Nivel" (puede contener 6000, FL120, etc.)
ROUTES_ALT_UNITS    = "ft"                       # "m" | "ft" | "fl"
ROUTES_LINE_WIDTH   = 3.0
ROUTES_LINE_COLOR   = "#00A0FF"                 # "#RRGGBB" o formato KML "AABBGGRR"
ROUTES_EXTRUDE      = False

# =========================
# CÓDIGO
# =========================

import re, os, math
import pandas as pd
from io import StringIO

# ---------- Utilidades generales ----------

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
        return None if n is None else n * 100.0 * 0.3048  # 1 FL = 100 ft
    n = _to_float(s)
    if n is None:
        # intenta detectar sufijos (ej: "6000 ft" / "180 FL")
        if s.endswith("FT"):
            n = _to_float(s[:-2])
            return None if n is None else n * 0.3048
        if s.startswith("FL"):
            n = _to_float(s[2:])
            return None if n is None else n * 100.0 * 0.3048
        return None
    if units == "ft":
        return n * 0.3048
    return n  # metros

def parse_dms_piece(piece):
    piece = re.sub(r"[NSEW]", "", piece, flags=re.I).strip().replace("º", "°")
    piece = re.sub(r"[°]", " ", piece).replace("'", " ").replace('"', " ").replace("’", " ").replace("′", " ")
    toks = [t for t in re.split(r"[\s:]+", piece.strip()) if t]
    if len(toks) == 3:
        deg, min_, sec = float(toks[0]), float(toks[1]), float(toks[2])
    elif len(toks) == 2:
        deg, min_, sec = float(toks[0]), float(toks[1]), 0.0
    elif len(toks) == 1:
        try:
            return float(toks[0].replace(",", "."))
        except:
            raise ValueError("Cannot parse DMS piece")
    else:
        raise ValueError("Cannot parse DMS piece")
    return deg + (min_ / 60.0) + (sec / 3600.0)

def parse_compact_dms(piece, guess_lon=False):
    """DDMMSS.SN / DDDMMSS.SW"""
    s = piece.strip().upper()
    m = re.match(r"^([0-9.]+)\s*([NSEW])$", s)
    m2 = re.match(r"^([NSEW])\s*([0-9.]+)$", s)
    hem = None; core = None
    if m: core, hem = m.group(1), m.group(2)
    elif m2: hem, core = m2.group(1), m2.group(2)
    else: core = re.sub(r"[^0-9.]", "", s)
    if not core: raise ValueError("No numeric core")
    if "." in core: left, frac = core.split(".",1); frac="."+frac
    else: left, frac = core, ""
    deg_len = 2 if len(left) in (6,7) else 3 if len(left) in (7,8,9) else (3 if guess_lon else 2)
    if len(left) < deg_len+4: left = left.rjust(deg_len+4, "0")
    deg=float(left[:deg_len]); mm=float(left[deg_len:deg_len+2]); ss=float(left[deg_len+2:deg_len+4])
    if frac: ss=float(f"{int(ss)}{frac}")
    val = deg + mm/60.0 + ss/3600.0
    if hem in ("S","W"): val = -val
    return val

def to_decimal(coord_str, context="latlon"):
    if coord_str is None or (isinstance(coord_str, float) and math.isnan(coord_str)):
        return None
    if isinstance(coord_str, (int, float)):
        return float(coord_str)
    s = str(coord_str).strip().replace(",", ".")
    # simple float (sin hemisferio explícito)
    try:
        if not re.search(r"[NSEW]$", s, flags=re.I):
            return float(s)
    except:
        pass
    hemi_match = re.search(r"([NSEW])", s, flags=re.I)
    hemi = hemi_match.group(1).upper() if hemi_match else None
    # caso combinado "lat, lon"
    if any(sep in s for sep in [",", ";", " "]):
        toks = [t for t in re.split(r"[;,]\s*|\s{1,}", s) if re.search(r"\d", t)]
        if len(toks) == 2:
            lat_val = to_decimal(toks[0], "lat")
            lon_val = to_decimal(toks[1], "lon")
            return ("PAIR", lat_val, lon_val)
    # DMS con símbolos
    if re.search(r"[°'\"′]|:", s):
        val = parse_dms_piece(s)
        if hemi in ("S","W"): val = -abs(val)
        elif hemi in ("N","E"): val = abs(val)
        return val
    # formato compacto
    try:
        return parse_compact_dms(s, guess_lon=(context=="lon"))
    except:
        pass
    # fallback: quitar no numéricos
    s2 = re.sub(r"[^0-9\.\-]", "", s)
    try:
        return float(s2)
    except:
        return None

def detect_coord_columns(df):
    cols = list(df.columns); low = [c.lower() for c in cols]
    lat = [i for i,c in enumerate(low) if "lat" in c]
    lon = [i for i,c in enumerate(low) if ("lon" in c) or ("long" in c)]
    combo = [i for i,c in enumerate(low) if ("coord" in c) or ("wgs" in c) or ("geog" in c)]
    combo += [i for i,c in enumerate(low) if c in ("position","pos","location")]
    lat_col = cols[lat[0]] if lat else None
    lon_col = cols[lon[0]] if lon else None
    combo_col = cols[combo[0]] if (not lat_col or not lon_col) and combo else None
    return lat_col, lon_col, combo_col

def pick_name_column(df):
    if POINTS_NAME_COLUMN and POINTS_NAME_COLUMN in df.columns:
        return POINTS_NAME_COLUMN
    low = {c.lower(): c for c in df.columns}
    for key in ["name","designation","designator","identifier","id","navaid","aerodrome","station"]:
        if key in low:
            return low[key]
    for c in df.columns:
        lc = c.lower()
        if not any(k in lc for k in ["lat","lon","long","coord","wgs","geog","position","pos","location"]):
            return c
    return df.columns[0]

def pick_type_column(df):
    if POINTS_TYPE_COLUMN and POINTS_TYPE_COLUMN in df.columns:
        return POINTS_TYPE_COLUMN
    low = {c.lower(): c for c in df.columns}
    for key in ["type","class","category","kind","usage"]:
        if key in low:
            return low[key]
    return None

def dataframe_from_csv(path):
    last_err = None
    for sep in [None, ",",";","\t","|"]:
        try:
            return pd.read_csv(path, sep=sep, engine="python", encoding="utf-8-sig")
        except Exception as e:
            last_err = e
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        text = f.read()
    try:
        return pd.read_csv(StringIO(text), engine="python")
    except Exception as e:
        raise last_err or e

def kml_color_from_hex(hex_str, alpha="ff"):
    """#RRGGBB -> aabbggrr (KML). Si ya viene AABBGGRR, se respeta."""
    s = str(hex_str).strip().lstrip("#")
    if len(s) == 8:
        return s.lower()
    if len(s) != 6:
        return "ff0000ff"  # rojo
    rr, gg, bb = s[0:2], s[2:4], s[4:6]
    return (alpha + bb + gg + rr).lower()

# ---------- Construcción de KML ----------

def build_points_placemarks(df, *, alt_mode, alt_fixed, alt_column, alt_units, extrude):
    lat_col, lon_col, combo_col = detect_coord_columns(df)
    name_col = pick_name_column(df)
    type_col = pick_type_column(df)
    types = sorted(df[type_col].dropna().unique().tolist()) if type_col and type_col in df.columns else []

    parts = []

    # Estilos por tipo (opcionales)
    type_style_ids = {}
    if types:
        style_defs = [
            ("http://maps.google.com/mapfiles/kml/paddle/red-circle.png", "ff0000ff"),
            ("http://maps.google.com/mapfiles/kml/paddle/grn-circle.png", "ff00ff00"),
            ("http://maps.google.com/mapfiles/kml/paddle/ylw-circle.png", "ff00ffff"),
            ("http://maps.google.com/mapfiles/kml/paddle/blu-circle.png", "ffff0000"),
        ]
        for i, t in enumerate(types):
            icon, color = style_defs[i % len(style_defs)]
            sid = f"type_{i}"
            type_style_ids[t] = sid
            parts.append(
                f'<Style id="{sid}">'
                f'<IconStyle><color>{color}</color><scale>1.1</scale>'
                f'<Icon><href>{icon}</href></Icon></IconStyle>'
                f'<LabelStyle><scale>0.9</scale></LabelStyle>'
                f'</Style>'
            )

    def row_to_coords(row):
        if lat_col and lon_col:
            lat = to_decimal(row[lat_col], "lat"); lon = to_decimal(row[lon_col], "lon")
            return lat, lon
        elif combo_col:
            v = row[combo_col]; parsed = to_decimal(v, "latlon")
            if isinstance(parsed, tuple) and parsed and parsed[0] == "PAIR":
                return parsed[1], parsed[2]
            if isinstance(v, str):
                toks = [t for t in re.split(r"[;,]\s*|\s{1,}", v) if re.search(r"\d", t)]
                if len(toks) >= 2:
                    return to_decimal(toks[0], "lat"), to_decimal(toks[1], "lon")
        return None, None

    def altitude_for_row(row):
        # prioridad: alt_column > alt_fixed > 0
        alt_m = None
        if alt_column and alt_column in df.columns:
            alt_m = _parse_alt_value(row[alt_column], units=alt_units)
        if alt_m is None:
            alt_m = _parse_alt_value(alt_fixed, units=alt_units) if alt_fixed is not None else 0.0
        if alt_m is None:
            alt_m = 0.0
        return float(alt_m)

    def html_description(row):
        items = []
        for c in df.columns:
            v = row[c]
            if pd.isna(v): continue
            items.append(f"<tr><th style='text-align:left;padding-right:8px'>{str(c)}</th><td>{str(v)}</td></tr>")
        return "<![CDATA[<table>{}</table>]]>".format("".join(items))

    def write_placemark(row, style_id=None):
        lat, lon = row_to_coords(row)
        if lat is None or lon is None:
            return None
        name = str(row[name_col]) if name_col in row else "Item"
        desc = html_description(row)
        style_line = f"<styleUrl>#{style_id}</styleUrl>" if style_id else "<styleUrl>#default</styleUrl>"
        alt = altitude_for_row(row)
        extrude_tag = "<extrude>1</extrude>" if extrude else ""
        return (
            "<Placemark>"
            f"<name>{name}</name>"
            f"{style_line}"
            f"<description>{desc}</description>"
            "<Point>"
            f"<altitudeMode>{alt_mode}</altitudeMode>"
            f"{extrude_tag}"
            f"<coordinates>{lon:.8f},{lat:.8f},{alt:.2f}</coordinates>"
            "</Point>"
            "</Placemark>"
        )

    # Carpeta por tipo (si existe)
    if types:
        for t in types:
            parts.append(f"<Folder><name>{t}</name>")
            for _, row in df[df[type_col] == t].iterrows():
                pm = write_placemark(row, style_id=type_style_ids.get(t))
                if pm: parts.append(pm)
            parts.append("</Folder>")
        remaining = df[df[type_col].isna()] if type_col else pd.DataFrame()
        if not remaining.empty:
            parts.append("<Folder><name>Sin tipo</name>")
            for _, row in remaining.iterrows():
                pm = write_placemark(row)
                if pm: parts.append(pm)
            parts.append("</Folder>")
    else:
        for _, row in df.iterrows():
            pm = write_placemark(row)
            if pm: parts.append(pm)

    return "\n".join(parts)

def build_routes_placemarks(df_routes, *,
                            alt_mode, fixed_alt, alt_column, alt_units,
                            group_col, order_col, line_color, line_width, extrude):
    lat_col, lon_col, combo_col = detect_coord_columns(df_routes)

    color_kml = kml_color_from_hex(line_color, alpha="ff")
    parts = [f'<Style id="routeStyle"><LineStyle><color>{color_kml}</color><width>{line_width}</width></LineStyle></Style>']

    if group_col and group_col in df_routes.columns:
        groups = [(g, gdf.copy()) for g, gdf in df_routes.groupby(group_col)]
    else:
        groups = [("Ruta", df_routes.copy())]

    def sort_group(gdf):
        if order_col and order_col in gdf.columns:
            try:
                return gdf.sort_values(order_col, key=lambda s: pd.to_numeric(s, errors="coerce"))
            except Exception:
                return gdf.sort_values(order_col)
        return gdf

    for name, gdf in groups:
        gdf = sort_group(gdf)
        coords = []
        for _, row in gdf.iterrows():
            # coordenadas
            if lat_col and lon_col:
                lat = to_decimal(row[lat_col], "lat"); lon = to_decimal(row[lon_col], "lon")
            elif combo_col:
                parsed = to_decimal(row[combo_col], "latlon")
                if isinstance(parsed, tuple) and parsed and parsed[0] == "PAIR":
                    lat, lon = parsed[1], parsed[2]
                else:
                    lat = lon = None
            else:
                lat = lon = None
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            # altitud por vértice
            if alt_column and alt_column in gdf.columns:
                alt_m = _parse_alt_value(row[alt_column], units=alt_units)
            else:
                alt_m = _parse_alt_value(fixed_alt, units=alt_units) if fixed_alt is not None else 0.0
            if alt_m is None: alt_m = 0.0
            coords.append(f"{lon:.8f},{lat:.8f},{alt_m:.2f}")

        if len(coords) >= 2:
            extrude_tag = "<extrude>1</extrude>" if extrude else ""
            parts.append(
                "<Placemark>"
                f"<name>{name}</name>"
                "<styleUrl>#routeStyle</styleUrl>"
                "<LineString>"
                f"<altitudeMode>{alt_mode}</altitudeMode>"
                f"{extrude_tag}"
                "<coordinates>" + " ".join(coords) + "</coordinates>"
                "</LineString>"
                "</Placemark>"
            )

    return "\n".join(parts)

def build_full_kml(points_df=None, routes_df=None, title="KML"):
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<kml xmlns="http://www.opengis.net/kml/2.2">')
    parts.append("<Document>")
    parts.append(f"<name>{title}</name>")

    # Estilo por defecto para puntos
    parts.append(
        '<Style id="default">'
        '<IconStyle><color>ff0000ff</color><scale>1.1</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/paddle/wht-blank.png</href></Icon>'
        '</IconStyle><LabelStyle><scale>0.9</scale></LabelStyle>'
        '</Style>'
    )

    if points_df is not None and POINTS_GENERATE:
        parts.append(
            build_points_placemarks(
                points_df,
                alt_mode=POINTS_ALT_MODE,
                alt_fixed=POINTS_ALT_FIXED,
                alt_column=POINTS_ALT_COLUMN,
                alt_units=POINTS_ALT_UNITS,
                extrude=POINTS_EXTRUDE,
            )
        )

    if routes_df is not None and ROUTES_GENERATE:
        parts.append(
            build_routes_placemarks(
                routes_df,
                alt_mode=ROUTES_ALT_MODE,
                fixed_alt=ROUTES_ALT_FIXED,
                alt_column=ROUTES_ALT_COLUMN,
                alt_units=ROUTES_ALT_UNITS,
                group_col=ROUTES_GROUP_COL,
                order_col=ROUTES_ORDER_COL,
                line_color=ROUTES_LINE_COLOR,
                line_width=ROUTES_LINE_WIDTH,
                extrude=ROUTES_EXTRUDE,
            )
        )

    parts.append("</Document>")
    parts.append("</kml>")
    return "\n".join(parts)

# ---------- Main ----------

def main():
    df_points = dataframe_from_csv(POINTS_CSV) if (POINTS_CSV and POINTS_GENERATE) else None
    df_routes = dataframe_from_csv(ROUTES_CSV) if (ROUTES_CSV and ROUTES_GENERATE) else None
    kml = build_full_kml(df_points, df_routes, title=KML_TITLE)
    with open(KML_OUTPUT, "w", encoding="utf-8") as f:
        f.write(kml)
    print(f"KML guardado en: {os.path.abspath(KML_OUTPUT)}")

if __name__ == "__main__":
    main()