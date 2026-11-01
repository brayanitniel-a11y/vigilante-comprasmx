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


def titulo(ref: str, datos: dict) -> str:
    tipo = (ref or "").upper().split("-")[0]
    partes = [PREFIJOS.get(tipo, tipo), nombre_corto(ref), identificador(datos)]
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


@dataclass
class Plan:
    titulo: str
    lista: str
    etiquetas: list[str]
    fecha_tarea: datetime | None
    items: list[Item]
    avisos: list[str] = field(default_factory=list)


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

    etiquetas = [pl["etiqueta"]]
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
            items.append(Item(it["titulo"], hecho=bool(cierre_ja)))
        elif k in auto:
            fecha, dia = auto[k]
            items.append(Item(it["titulo"], fecha, dia))
        else:
            items.append(Item(it["titulo"]))
    for nombre, v in (("la junta de aclaraciones", ja), ("la presentación y apertura", pp), ("el fallo", nf)):
        if not v:
            avisos.append(f"ComprasMX no trae la fecha de {nombre}")
    return Plan(titulo(ref, datos), pl["lista"], etiquetas, fecha_tarea, items, avisos)


def texto_plan(plan: Plan) -> str:
    """Resumen legible (sin formato HTML)."""
    lineas = [plan.titulo,
              f"Lista: {plan.lista} | Etiquetas: {', '.join(plan.etiquetas)}",
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
