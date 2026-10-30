"""
Lectura de anexos/actas desde la consulta publica de ComprasMX.

Usa un navegador real (Playwright) porque el sitio firma sus peticiones internas.
Un solo navegador por ciclo, se bloquean imagenes/medios.

Como esta hecha la pagina del procedimiento (verificado con un procedimiento real):
es UNA sola hoja larga con secciones (.titulo-seccion). La que importa es "ANEXOS", una tabla
con columnas: Num. | Tipo de documento | Descripcion | Total de Archivos | Acciones.
Cada fila es una categoria (CONVOCATORIA, ANEXO TECNICO, ...) y "Total de Archivos" sube
cuando publican un archivo nuevo en ella. Si aparece una seccion de actas (o filas nuevas de
tipo "ACTA ..."), tambien se detecta.
"""
import hashlib
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from cronograma import parse_pairs

BASE_URL = os.environ.get("COMPRASMX_URL", "https://comprasmx.buengobierno.gob.mx/sitiopublico/#/")
TABS = [t.strip() for t in os.environ.get(
    "TABS", "Anuncios vigentes,Anuncios en seguimiento,Anuncios concluidos").split(",") if t.strip()]
DEBUG_DIR = Path(os.environ.get("DEBUG_DIR", "debug"))
DATE_RE = r"\d{2}/\d{2}/\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"
EMPTY_RE = re.compile(r"no hay|sin registros|no se encontr|no existen|no cuenta con", re.I)
ACTA_RE = re.compile(r"acta|junta|fallo|apertura", re.I)

# Extrae, en el navegador, los titulos de seccion y las tablas de cada seccion.
JS_TABLAS = """() => {
  const titles = [], tables = [];
  document.querySelectorAll('.titulo-seccion').forEach(t => {
    const sec = (t.innerText || '').trim();
    titles.push(sec);
    const box = t.parentElement;
    if (!box) return;
    box.querySelectorAll('table').forEach(tb => {
      const ths = [...tb.querySelectorAll('thead th')].map(x => (x.innerText || '').trim());
      const rows = [...tb.querySelectorAll('tbody tr')].map(
        tr => [...tr.querySelectorAll('td')].map(td => (td.innerText || '').trim()));
      tables.push({sec, ths, rows});
    });
  });
  return {titles, tables};
}"""


JS_FILAS_ANEXOS = """() => {
  const c = document.querySelector('app-sitiopublico-detalle-anexos');
  if (!c) return [];
  return [...c.querySelectorAll('tbody tr')]
    .map(tr => [...tr.querySelectorAll('td')].map(td => (td.innerText || '').trim()))
    .filter(r => r.length >= 4 && /^\\d+$/.test(r[0]));
}"""

JS_CAMBIO_PRIMERA = """(prev) => {
  const c = document.querySelector('app-sitiopublico-detalle-anexos');
  const tr = c && c.querySelector('tbody tr');
  const td = tr && tr.querySelector('td');
  return !!td && (td.innerText || '').trim() !== prev;
}"""


JS_CRONOGRAMA = """() => {
  const t = [...document.querySelectorAll('.titulo-seccion')].find(x => /CRONOGRAMA/i.test(x.innerText || ''));
  if (!t || !t.parentElement) return null;
  return [...t.parentElement.querySelectorAll('label.font-bold')].map(l => {
    const v = l.parentElement ? l.parentElement.querySelector('label:not(.font-bold)') : null;
    return [(l.innerText || '').trim(), v ? (v.innerText || '').trim() : ''];
  });
}"""

JS_CRONOGRAMA_LISTO = """() => {
  const t = [...document.querySelectorAll('.titulo-seccion')].find(x => /CRONOGRAMA/i.test(x.innerText || ''));
  return !!t && !!t.parentElement && t.parentElement.querySelectorAll('label.font-bold').length > 0;
}"""


class Lectura(list):
    """Lista de Anexo + el cronograma de eventos leido en la misma pagina (dict, o None si no se pudo leer)."""
    cronograma = None


class NoEncontrado(Exception):
    """El numero de procedimiento no aparece en la consulta publica."""


@dataclass(frozen=True)
class Anexo:
    id: str
    nombre: str
    url: str = ""
    fecha: str = ""
    grupo: str = ""   # identifica la fila (seccion|tipo|descripcion), sin el total
    total: int = 0    # total de archivos de esa fila (0 si no aplica)


def parse_tablas(raw: dict, proc_url: str = "") -> list[Anexo]:
    """Convierte lo extraido de la pagina en la lista de anexos/actas. Funcion pura (se puede probar sin navegador)."""
    docs: list[Anexo] = []
    vistos: set[str] = set()
    for t in raw.get("tables", []):
        sec = t["sec"]
        ths = [h.lower() for h in t["ths"]]

        def col(pat: str) -> int:
            for i, h in enumerate(ths):
                if re.search(pat, h):
                    return i
            return -1

        i_num, i_tipo, i_desc, i_tot = col(r"n[uú]m"), col(r"tipo de documento"), col(r"descripci"), col(r"total de archivos")
        es_doc = i_tipo >= 0
        if not es_doc and not ACTA_RE.search(sec):
            continue  # partidas, requerimientos economicos, etc.
        for cells in t["rows"]:
            plano = " ".join(" ".join(cells).split())
            if not plano or EMPTY_RE.search(plano):
                continue
            if es_doc:
                tipo = cells[i_tipo] if i_tipo < len(cells) else ""
                desc = cells[i_desc] if 0 <= i_desc < len(cells) else ""
                tot = cells[i_tot] if 0 <= i_tot < len(cells) else ""
            else:
                partes = [c for c in cells if c]
                tipo = partes[0] if partes else plano
                desc = " | ".join(partes[1:3])
                tot = ""
            m = re.search(DATE_RE, plano)
            fecha = m.group(0) if m else ""
            num = cells[i_num] if 0 <= i_num < len(cells) else ""
            grupo = f"{sec}|{num}|{tipo}|{desc}"
            uid = hashlib.sha1(f"{grupo}|{tot}".encode()).hexdigest()[:16]
            if uid in vistos:
                continue
            vistos.add(uid)
            nombre = f"[{sec}] {tipo}"
            if desc and desc.strip().lower() != tipo.strip().lower():
                nombre += f" — {desc}"
            if tot:
                nombre += f" ({tot} archivos)"
            docs.append(Anexo(uid, nombre, proc_url, fecha, grupo, int(tot) if tot.isdigit() else 0))
    return docs


class Browser:
    """Uso:  with Browser() as fetch:  docs = fetch("LA-07-110-...")"""

    def __init__(self, debug: bool = False):
        self.debug = debug or os.environ.get("DEBUG") == "1"

    # ----- ciclo de vida ---------------------------------------------------
    def __enter__(self):
        from playwright.sync_api import sync_playwright  # import perezoso

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True, args=["--no-sandbox"])
        self._ctx = self._browser.new_context(
            locale="es-MX", timezone_id="America/Mexico_City",
            viewport={"width": 1366, "height": 900},
        )
        self._ctx.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media")
            else route.continue_(),
        )
        self.page = self._ctx.new_page()
        self.page.set_default_timeout(20000)
        return self

    def __exit__(self, *exc):
        for closer in (self._ctx.close, self._browser.close, self._pw.stop):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    def __call__(self, numero: str) -> list[Anexo]:
        return self.anexos(numero)

    # ----- utilidades --------------------------------------------------------
    def _settle(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)

    def _first(self, candidates, timeout=3000):
        for loc in candidates:
            try:
                loc.first.wait_for(state="visible", timeout=timeout)
                return loc.first
            except Exception:  # noqa: BLE001
                continue
        return None

    def _dump(self, tag: str):
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", tag)
            self.page.screenshot(path=str(DEBUG_DIR / f"{safe}.png"), full_page=True)
            (DEBUG_DIR / f"{safe}.html").write_text(self.page.content(), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print("no pude guardar debug:", e)

    # ----- pasos -----------------------------------------------------------
    def _buscar(self, numero: str, tab: str):
        p = self.page
        p.goto(BASE_URL, wait_until="domcontentloaded")
        self._settle()
        if tab:
            t = self._first([
                p.get_by_role("tab", name=re.compile(re.escape(tab), re.I)),
                p.get_by_text(re.compile(re.escape(tab), re.I)),
            ])
            if t:
                t.click()
                self._settle()
        caja = self._first([
            p.get_by_label(re.compile(r"n[uú]mero de procedimiento", re.I)),
            p.get_by_placeholder(re.compile(r"procedimiento", re.I)),
            p.locator("xpath=//*[contains(normalize-space(.),'mero de procedimiento')]/following::input[1]"),
        ])
        if not caja:
            self._dump("sin_campo_numero")
            raise RuntimeError("No encontre el campo 'Numero de procedimiento' en la consulta publica")
        caja.fill(numero)
        boton = self._first([p.get_by_role("button", name=re.compile(r"buscar|consultar", re.I))], 2000)
        if boton:
            boton.click()
        else:
            caja.press("Enter")
        self._settle()
        if self.debug:
            self._dump(f"busqueda_{tab or 'inicio'}")

    def _abrir(self, numero: str) -> bool:
        p = self.page
        link = self._first([
            p.get_by_role("link", name=re.compile(re.escape(numero), re.I)),
            p.get_by_text(numero, exact=False),
        ], 4000)
        if not link:
            return False
        link.click()
        self._settle()
        return True

    def _leer_documentos(self, proc_url: str) -> list[Anexo]:
        p = self.page
        try:
            p.wait_for_selector(".titulo-seccion", timeout=20000)
        except Exception:  # noqa: BLE001
            self._dump("detalle_no_cargo")
            raise RuntimeError("La pagina del procedimiento no termino de cargar")
        # Mientras carga, la tabla muestra una fila "No se encontraron resultados" y "Total: 0":
        # eso NO es una tabla vacia de verdad. Esperamos a filas reales (primera celda numerica).
        filas_reales = """() => {
          const c = document.querySelector('app-sitiopublico-detalle-anexos');
          if (!c) return false;
          return [...c.querySelectorAll('tbody tr')].some(tr => {
            const td = tr.querySelectorAll('td');
            return td.length >= 4 && /^\\d+$/.test((td[0].innerText || '').trim());
          });
        }"""
        cargo = False
        for espera in (25000, 10000):  # si no aparecen filas, una segunda oportunidad antes de darla por vacia
            try:
                p.wait_for_function(filas_reales, timeout=espera)
                cargo = True
                break
            except Exception:  # noqa: BLE001
                continue
        self._settle()
        raw = p.evaluate(JS_TABLAS)
        if cargo:
            todas = self._filas_anexos_todas_las_paginas()
            for t in raw.get("tables", []):
                if t["sec"].strip().upper() == "ANEXOS" and any("tipo de documento" in h.lower() for h in t["ths"]):
                    if todas:
                        t["rows"] = todas
        if not cargo and self.debug:
            print("aviso: la tabla de anexos no mostro filas (puede estar vacia de verdad o cargar lento)")
        # La tabla de anexos tiene paginador ("Total: N"): recorre todas las paginas.
        paginas = [raw]
        for _ in range(20):
            sig = p.locator("app-sitiopublico-detalle-anexos .p-paginator-next:not(.p-disabled)")
            if sig.count() == 0:
                break
            sig.first.click()
            self._settle()
            paginas.append(p.evaluate(JS_TABLAS))
        raw = {"titles": raw.get("titles", []), "tables": [t for r in paginas for t in r.get("tables", [])]}
        titulos = " | ".join(raw.get("titles", []))
        if not re.search(r"anexos", titulos, re.I) and not ACTA_RE.search(titulos):
            self._dump("sin_seccion_anexos")
            raise RuntimeError(f"No encontre la seccion ANEXOS (secciones vistas: {titulos[:150]})")
        try:  # el cronograma se pinta junto con la pagina; espera corta y no fatal
            p.wait_for_function(JS_CRONOGRAMA_LISTO, timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        lectura = Lectura(parse_tablas(raw, proc_url))
        pares = p.evaluate(JS_CRONOGRAMA)
        lectura.cronograma = parse_pairs(pares) if pares else None
        return lectura

    def _filas_anexos_todas_las_paginas(self) -> list[list[str]]:
        """Recorre TODAS las paginas de la tabla ANEXOS (10 filas por pagina) y devuelve todas las filas."""
        p = self.page
        pag = p.locator("app-sitiopublico-detalle-anexos .p-paginator")
        if pag.count() == 0:
            return p.evaluate(JS_FILAS_ANEXOS)

        def boton(sel):
            b = pag.first.locator(sel)
            if b.count() == 0:
                return None
            b = b.first
            if "p-disabled" in (b.get_attribute("class") or "") or b.is_disabled():
                return None
            return b

        def primera() -> str:
            f = p.evaluate(JS_FILAS_ANEXOS)
            return f[0][0] if f else ""

        def ir(b):
            prev = primera()
            b.click()
            try:
                p.wait_for_function(JS_CAMBIO_PRIMERA, arg=prev, timeout=8000)
            except Exception:  # noqa: BLE001
                return False
            time.sleep(0.3)
            return True

        b = boton(".p-paginator-first")
        if b:
            ir(b)  # siempre empezar en la pagina 1
        todas: list[list[str]] = []
        vistas: set[str] = set()
        for _ in range(60):
            for r in p.evaluate(JS_FILAS_ANEXOS):
                if r[0] not in vistas:
                    vistas.add(r[0])
                    todas.append(r)
            nxt = boton(".p-paginator-next")
            if not nxt or not ir(nxt):
                break
        return todas

    # ----- API publica -----------------------------------------------------
    def anexos(self, numero: str) -> list[Anexo]:
        try:
            for tab in TABS or [""]:
                self._buscar(numero, tab)
                if self._abrir(numero):
                    break
            else:
                raise NoEncontrado(numero)
            proc_url = self.page.url
            docs = self._leer_documentos(proc_url)
            if self.debug:
                self._dump(f"detalle_{numero}")
            time.sleep(random.uniform(0.3, 0.8))
            return docs
        except NoEncontrado:
            raise
        except Exception:
            self._dump(f"error_{numero}")
            raise
