"""
Funciones puras para el CRONOGRAMA DE EVENTOS de un procedimiento (se pueden probar sin navegador).

La pagina muestra pares "etiqueta: valor" (por ejemplo
"Fecha y hora de presentación y apertura de proposiciones:" -> "06/11/2026 11:00").
Guardamos todos los pares tal como aparecen y vigilamos solo los de VIGILADAS.
"""
import re
from datetime import datetime

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
FECHA_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")

# Fechas que avisan cuando cambian: (patron de la etiqueta, nombre que sale en el aviso).
# Para vigilar otra (por ejemplo la junta de aclaraciones o el fallo) basta agregar una linea aqui.
VIGILADAS = [
    (re.compile(r"^fecha\b.*apertura de proposiciones", re.I), "presentación y apertura de proposiciones"),
]

# Fechas que se muestran en el resumen de /fechas
RESUMEN = [
    ("Publicación", re.compile(r"^fecha\b.*publicaci", re.I)),
    ("Junta de aclaraciones", re.compile(r"^fecha y hora de junta", re.I)),
    ("Presentación y apertura", re.compile(r"^fecha\b.*apertura de proposiciones", re.I)),
    ("Fallo", re.compile(r"^fecha\b.*fallo", re.I)),
]


def parse_pairs(pairs) -> dict[str, str]:
    """[[etiqueta, valor], ...] -> {etiqueta sin ':' : valor limpio}. Ignora etiquetas vacias."""
    out: dict[str, str] = {}
    for par in pairs or []:
        if len(par) < 2:
            continue
        k = " ".join(str(par[0]).split()).rstrip(":").strip()
        v = " ".join(str(par[1]).split())
        if k and k not in out:
            out[k] = v
    return out


def parse_fecha(v: str):
    m = FECHA_RE.search(v or "")
    if not m:
        return None
    d, mo, y, h, mi = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0))
    except ValueError:
        return None


def fmt_fecha(v: str) -> str:
    """'06/11/2026 11:00' -> 'viernes 06/11/2026 11:00' (si no es una fecha, regresa el texto tal cual)."""
    dt = parse_fecha(v)
    if not dt:
        return v or "(sin dato)"
    con_hora = bool(re.search(r"\d{1,2}:\d{2}", v))
    return f"{DIAS[dt.weekday()]} {dt:%d/%m/%Y}" + (f" {dt:%H:%M}" if con_hora else "")


def buscar(crono: dict, patron):
    """Primera etiqueta que cumple el patron y tiene una fecha valida -> (etiqueta, valor) o (None, None)."""
    for k, v in crono.items():
        if patron.search(k) and parse_fecha(v):
            return k, v
    return None, None


def es_lectura_valida(crono) -> bool:
    """Una lectura a medio cargar no trae ninguna fecha: no debe tocar el estado ni avisar."""
    return bool(crono) and any(parse_fecha(v) for v in crono.values())


def cambios(antes: dict, ahora: dict):
    """Devuelve [(nombre, valor_anterior, valor_nuevo)] solo de las fechas vigiladas que cambiaron."""
    out = []
    for patron, nombre in VIGILADAS:
        _, a = buscar(antes, patron)
        _, b = buscar(ahora, patron)
        if a is None or b is None:
            continue  # sin dato anterior (primera vez) o la pagina no lo trajo: no se avisa
        if parse_fecha(a) != parse_fecha(b):
            out.append((nombre, a, b))
    return out


def mezclar(antes: dict, ahora: dict) -> dict:
    """Estado nuevo: lo que trajo la pagina, sin pisar con vacios lo que ya sabiamos."""
    r = dict(antes or {})
    for k, v in ahora.items():
        if v:
            r[k] = v
    return r


def describir_cambio(nombre: str, antes: str, ahora: str) -> str:
    texto = f"Se ha cambiado la fecha de {nombre}, del {fmt_fecha(antes)} al {fmt_fecha(ahora)}."
    a, b = parse_fecha(antes), parse_fecha(ahora)
    if a and b:
        dias = (b.date() - a.date()).days
        if dias > 0:
            texto += f"\nDiferida {dias} día{'s' if dias != 1 else ''}."
        elif dias < 0:
            texto += f"\nAdelantada {-dias} día{'s' if dias != -1 else ''}."
        else:
            texto += "\nEs el mismo día; cambia la hora."
    return texto


def resumen(crono: dict) -> list[tuple[str, str]]:
    """Las fechas principales, ya con formato, para /fechas."""
    out = []
    for titulo, patron in RESUMEN:
        _, v = buscar(crono, patron)
        if v:
            out.append((titulo, fmt_fecha(v)))
    return out
