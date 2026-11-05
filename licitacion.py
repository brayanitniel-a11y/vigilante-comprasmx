"""
Reglas de la tarea de una licitacion (funciones puras, sin red): titulo, identificador de lugar,
fechas del checklist y fecha de la tarea. Lo usa /plan y, mas adelante, la integracion con TickTick.

Reglas (confirmadas por el usuario):
- Titulo: "<TIPO> <nombre corto> <identificador>", p. ej. "LICITACIÓN LA-T-163-2026 Baja California".
  nombre corto = tipo de procedimiento + caracter + consecutivo + anio. Identificador = SEDENA si es de Defensa
  (ramo 07), si no la entidad federativa (CDMX y EDOMEX abreviados).
- "Limite para envío de Preguntas" = junta de aclaraciones (JA) - 25 h.
  Si ComprasMX trae un limite propio mas temprano, se usa ese menos 1 h (para no dejar fuera al usuario).
- "Elaborar Preguntas" = el dia anterior al limite, sin hora.
- "EVENTO: JA", "Evento: PP" y "Evento: NF" toman sus fechas del cronograma de ComprasMX.
- Fecha de la tarea = PP; cuando pasa la hora de PP cambia a NF y se agrega la etiqueta FALLO.
- "Acta de cierre de JA" = casilla sin fecha; se marca cuando se publica el acta de cierre (ya no habra mas
  cambios en bases ni fechas).
El orden y los textos del checklist viven en plantilla.json.
"""
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import cronograma as cr

PLANTILLA_PATH = Path(__file__).with_name("plantilla.json")

PREFIJOS = {"LA": "LICITACIÓN", "LO": "LICITACIÓN", "IA": "INVITACIÓN", "IO": "INVITACIÓN",
            "AA": "ADJUDICACIÓN", "AO": "ADJUDICACIÓN"}
ESPECIALES = {"CIUDAD DE MEXICO": "CDMX", "MEXICO": "EDOMEX", "ESTADO DE MEXICO": "EDOMEX"}
PARTICULAS = {"de", "del", "la", "las", "los", "y", "e"}

# Etiqueta de ComprasMX -> clave corta con la que se guarda
DATOS_CLAVES = [
    ("estatus", re.compile(r"^estatus del procedimiento", re.I)),
    ("dependencia", re.compile(r"^dependencia o entidad", re.I)),
    ("ramo", re.compile(r"^ramo$", re.I)),
    ("unidad", re.compile(r"^unidad compradora$", re.I)),
    ("nombre", re.compile(r"^nombre del procedimiento", re.I)),
    ("tipo", re.compile(r"^tipo de procedimiento", re.I)),
    ("entidad", re.compile(r"^entidad federativa", re.I)),
    ("caracter", re.compile(r"^car[aá]cter$", re.I)),
]


def cargar_plantilla(path: Path = PLANTILLA_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def extraer_datos(todos: dict) -> dict:
    """De todos los pares etiqueta/valor de la pagina deja solo los que usamos, con clave corta."""
    out = {}
    for clave, pat in DATOS_CLAVES:
        for k, v in (todos or {}).items():
            if pat.search(k.strip()) and v:
                out[clave] = v
                break
    return out


def nombre_corto(ref: str) -> str:
    """LA-50-GYR-050GYR003-T-163-2026 -> LA-T-163-2026"""
    p = (ref or "").strip().upper().split("-")
    if len(p) >= 5 and p[-3] in ("N", "T", "I") and re.fullmatch(r"\d{4}", p[-1]) and p[-2].isdigit():
        return f"{p[0]}-{p[-3]}-{p[-2]}-{p[-1]}"
    return (ref or "").strip().upper()


def _capitalizar(texto: str) -> str:
    palabras = texto.lower().split()
    return " ".join(
        w if (i and w in PARTICULAS) else w[:1].upper() + w[1:] for i, w in enumerate(palabras)
    )


def identificador(datos: dict) -> str:
    """SEDENA si es de Defensa; si no, la entidad federativa (CDMX / EDOMEX abreviados)."""
    datos = datos or {}
    dep = _sin_acentos(f"{datos.get('dependencia', '')} {datos.get('ramo', '')}")
    if re.search(r"defensa nacional", dep, re.I) or str(datos.get("ramo", "")).strip().startswith("07"):
        return "SEDENA"
    ent = (datos.get("entidad") or "").strip()
    if not ent:
        return ""
    clave = _sin_acentos(ent).upper()
    return ESPECIALES.get(clave) or _capitalizar(ent)


def prefijo(ref: str, datos: dict) -> str:
    """Prefijo segun la clasificacion del portal ("Tipo de procedimiento"); si no la trae, segun las siglas."""
    t = _sin_acentos((datos or {}).get("tipo", "")).upper()
    for clave, pref in (("LICITACION", "LICITACIÓN"), ("INVITACION", "INVITACIÓN"), ("ADJUDICACION", "ADJUDICACIÓN")):
        if clave in t:
            return pref
    sig = (ref or "").upper().split("-")[0]
    return PREFIJOS.get(sig, sig)


def caracter(ref: str, datos: dict) -> str:
    """Nacional / Internacional bajo tratados / Internacional abierta (del portal; si falta, de la letra N-T-I)."""
    c = _sin_acentos((datos or {}).get("caracter", "")).upper()
    if "TRATADOS" in c:
        return "Internacional bajo tratados"
    if "ABIERT" in c:
        return "Internacional abierta"
    if c.startswith("NACIONAL"):
        return "Nacional"
    letra = (nombre_corto(ref).split("-") + ["", ""])[1]
    return {"N": "Nacional", "T": "Internacional bajo tratados", "I": "Internacional abierta"}.get(letra, "")


def titulo(ref: str, datos: dict) -> str:
    partes = [prefijo(ref, datos), nombre_corto(ref), identificador(datos)]
    return " ".join(p for p in partes if p)


def desambiguar(titulos: dict[str, str]) -> dict[str, str]:
    """Si dos procedimientos vigilados dan el mismo titulo, se agrega la clave de la unidad compradora."""
    cuenta = Counter(titulos.values())
    out = {}
    for ref, t in titulos.items():
        p = ref.upper().split("-")
        uc = p[3] if len(p) >= 7 else ""
        out[ref] = f"{t} ({uc})" if cuenta[t] > 1 and uc else t
    return out


@dataclass
class Item:
    titulo: str
    fecha: datetime | date | None = None
    todo_el_dia: bool = False
    hecho: bool = False
    clave: str = ""        # tipo de punto automatico (ja, pp, nf, limite_preguntas, ...); vacio si es del usuario


@dataclass
class Plan:
    titulo: str
    lista: str
    etiquetas: list[str]
    fecha_tarea: datetime | None
    items: list[Item]
    avisos: list[str] = field(default_factory=list)
    caracter: str = ""
    estatus: str = ""      # estatus que muestra ComprasMX (EN APERTURA, EN ACLARACIONES...); va como etiqueta


def calcular(ref: str, crono: dict, datos: dict, ahora: datetime, cierre_ja: bool = False,
             plantilla: dict | None = None) -> Plan:
    """Todo se recalcula desde cero con los datos actuales: si ComprasMX difiere una fecha, el plan cambia solo."""
    pl = plantilla or cargar_plantilla()
    f = cr.fechas_clave(crono)
    ja, pp, nf, visita, lim_portal = f["ja"], f["pp"], f["nf"], f["visita"], f["limite"]

    limite = ja - timedelta(hours=pl.get("tolerancia_preguntas_horas", 25)) if ja else None
    if lim_portal:
        cand = lim_portal - timedelta(hours=1)
        limite = min(limite, cand) if limite else cand
    elaborar = limite.date() - timedelta(days=1) if limite else None

    etiquetas = [pl["etiqueta"]] if pl.get("etiqueta") else []   # sin etiqueta fija: solo FALLO y el estatus
    if pp and ahora < pp:
        fecha_tarea = pp
    elif nf:
        fecha_tarea = nf
        if pp and ahora >= pp:
            etiquetas.append(pl["etiqueta_fallo"])
    else:
        fecha_tarea = pp

    auto = {
        "elaborar_preguntas": (elaborar, True),
        "visita": (visita, False),
        "limite_preguntas": (limite, False),
        "ja": (ja, False),
        "pp": (pp, False),
        "nf": (nf, False),
    }
    items, avisos = [], []
    for it in pl["items"]:
        k = it.get("auto")
        if k == "cierre_ja":
            items.append(Item(it["titulo"], hecho=bool(cierre_ja), clave=k))
        elif k in auto:
            fecha, dia = auto[k]
            items.append(Item(it["titulo"], fecha, dia, clave=k))
        else:
            items.append(Item(it["titulo"]))
    for nombre, v in (("la junta de aclaraciones", ja), ("la presentación y apertura", pp), ("el fallo", nf)):
        if not v:
            avisos.append(f"ComprasMX no trae la fecha de {nombre}")
    return Plan(titulo(ref, datos), pl["lista"], etiquetas, fecha_tarea, items, avisos, caracter(ref, datos),
                (datos or {}).get("estatus", "").strip())


def texto_plan(plan: Plan) -> str:
    """Resumen legible (sin formato HTML)."""
    lineas = [plan.titulo,
              f"Lista: {plan.lista} | Etiquetas: {', '.join(plan.etiquetas) or '(ninguna)'}",
              f"Carácter: {plan.caracter or '(sin dato)'} | Estatus: {plan.estatus or '(sin dato)'}",
              f"Fecha de la tarea: {cr.fmt_dt(plan.fecha_tarea) if plan.fecha_tarea else '(sin fecha)'}",
              ""]
    for it in plan.items:
        marca = "☑" if it.hecho else "☐"
        if isinstance(it.fecha, datetime):
            cuando = f" — {cr.fmt_dt(it.fecha)}"
        elif it.fecha:
            cuando = f" — {cr.fmt_dt(datetime(it.fecha.year, it.fecha.month, it.fecha.day), False)} (sin hora)"
        else:
            cuando = ""
        lineas.append(f"{marca} {it.titulo}{cuando}")
    for a in plan.avisos:
        lineas.append(f"⚠ {a}")
    return "\n".join(lineas)


# ----------------------------------------------------------------------------------------------
# Recordatorio del dia
# ----------------------------------------------------------------------------------------------
ORDEN_EVENTOS = ["nf", "pp", "ja", "limite_preguntas", "elaborar_preguntas", "visita"]


def nombre_visible(ref: str, datos: dict, alias: str = "") -> str:
    """LA-N-23-2026 Hidalgo (alias)"""
    base = " ".join(p for p in (nombre_corto(ref), identificador(datos)) if p)
    return f"{base} ({alias})" if alias else base


def eventos_hoy(ref: str, crono: dict, datos: dict, hoy: date) -> list[tuple[str, datetime | date]]:
    """Eventos de este procedimiento que caen en `hoy`: [(clave, fecha)]. Usa las mismas reglas que la tarea."""
    plan = calcular(ref, crono, datos, datetime(hoy.year, hoy.month, hoy.day))
    out = []
    for it in plan.items:
        if it.clave in ORDEN_EVENTOS and it.fecha:
            d = it.fecha.date() if isinstance(it.fecha, datetime) else it.fecha
            if d == hoy:
                out.append((it.clave, it.fecha))
    return out


def _lista(nombres: list[str]) -> str:
    if len(nombres) <= 1:
        return "".join(nombres)
    return ", ".join(nombres[:-1]) + " y " + nombres[-1]


def _con_hora(nombre: str, fecha, prefijo: str = "", quienes: str = "") -> str:
    partes = []
    if isinstance(fecha, datetime):
        partes.append(f"{prefijo}{fecha:%H:%M}")
    if quienes:
        partes.append(f"a cargo de {quienes}")
    return f"{nombre} ({', '.join(partes)})" if partes else nombre


def texto_recordatorio(eventos: list[tuple], hoy: date) -> str:
    """eventos: [(nombre visible, clave, fecha[, quienes])]. Devuelve el mensaje en tono natural, o "" si no hay nada hoy.
    Los textos que llegan ya vienen listos para mostrar (el bot escapa el HTML y arma las menciones)."""
    if not eventos:
        return ""
    por_clave: dict[str, list[tuple]] = {}
    for nombre, clave, fecha, *resto in eventos:
        por_clave.setdefault(clave, []).append((nombre, fecha, resto[0] if resto else ""))

    def clave_orden(t):
        f = t[1]
        return f if isinstance(f, datetime) else datetime(f.year, f.month, f.day)

    def frase(clave: str) -> str:
        lista = sorted(por_clave[clave], key=clave_orden)
        n = len(lista)
        v = _lista([_con_hora(nom, f, "límite " if clave == "limite_preguntas" else "", q) for nom, f, q in lista])
        if clave == "nf":
            return (f"salen las actas de fallo de {v}, así que debes estar al pendiente" if n > 1
                    else f"sale el acta de fallo de {v}, así que debes estar al pendiente")
        if clave == "pp":
            return (f"son las presentaciones y aperturas de proposiciones de {v}" if n > 1
                    else f"es la presentación y apertura de proposiciones de {v}")
        if clave == "ja":
            return f"son las juntas de aclaraciones de {v}" if n > 1 else f"es la junta de aclaraciones de {v}"
        if clave == "limite_preguntas":
            return f"se deben cargar las preguntas de la junta de aclaraciones de {v}"
        if clave == "elaborar_preguntas":
            return f"toca elaborar las preguntas de la junta de aclaraciones de {v}"
        return f"hay visitas a instalaciones de {v}" if n > 1 else f"hay visita a instalaciones de {v}"

    oraciones = []
    for clave in [c for c in ORDEN_EVENTOS if c in por_clave]:
        inicio = "Recuerda que hoy " if not oraciones else "Además, hoy "
        oraciones.append(f"{inicio}{frase(clave)}.")
    return "\n\n".join(oraciones)
