"""
Lectura de anexos/actas desde la consulta publica de ComprasMX.

Usa un navegador real (Playwright) porque el sitio firma sus peticiones internas.
Un solo navegador por ciclo, se bloquean imagenes/medios y solo se abre la
seccion de anexos/actas: es lo mas ligero posible.

IMPORTANTE: los textos de botones/pestanas de abajo son lo unico que depende del
diseno del sitio. Si ComprasMX cambia algo, se ajustan aqui (ver README, "Calibrar").
"""
import hashlib
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

BASE_URL = os.environ.get("COMPRASMX_URL", "https://comprasmx.buengobierno.gob.mx/sitiopublico/#/")
TABS = [t.strip() for t in os.environ.get(
    "TABS", "Anuncios vigentes,Anuncios en seguimiento,Anuncios concluidos").split(",") if t.strip()]
SECTIONS = [s.strip() for s in os.environ.get("SECTIONS", "Anexos,Actas").split(",") if s.strip()]
DEBUG_DIR = Path(os.environ.get("DEBUG_DIR", "debug"))
DATE_RE = r"\d{2}/\d{2}/\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"
EMPTY_RE = re.compile(r"no hay|sin registros|no se encontr|no existen|no cuenta con", re.I)


class NoEncontrado(Exception):
    """El numero de procedimiento no aparece en la consulta publica."""


@dataclass(frozen=True)
class Anexo:
    id: str
    nombre: str
    url: str = ""
    fecha: str = ""


class Browser:
    """Uso:  with Browser() as fetch:  docs = fetch("IO-89-L47-...")"""

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

    def _leer_seccion(self, seccion: str, proc_url: str) -> list[Anexo]:
        p = self.page
        exacto = re.compile(rf"^\s*{re.escape(seccion)}\s*$", re.I)
        pestana = self._first([
            p.get_by_role("tab", name=exacto),
            p.get_by_role("link", name=exacto),
            p.get_by_text(exacto),
        ], 3000)
        if not pestana:
            return []  # este procedimiento aun no tiene esa seccion
        pestana.click()
        self._settle()
        if self.debug:
            self._dump(f"seccion_{seccion}")

        salida: list[Anexo] = []
        for _ in range(10):  # maximo 10 paginas
            filas = p.locator("table tbody tr, mat-row, tr.mat-row")
            n = filas.count()
            if n == 0 and not salida:
                if EMPTY_RE.search(p.inner_text("body")):
                    return []
                self._dump(f"sin_tabla_{seccion}")
                raise RuntimeError(f"No pude leer la tabla de '{seccion}'")
            for i in range(n):
                texto = filas.nth(i).inner_text()
                plano = " ".join(texto.split())
                if not plano or EMPTY_RE.search(plano):
                    continue
                celdas = [c.strip() for c in re.split(r"[\t\n]+", texto) if c.strip()]
                m = re.search(DATE_RE, plano)
                fecha = m.group(0) if m else ""
                nombre = next((c for c in celdas if not re.fullmatch(DATE_RE, c) and len(c) > 3), plano)
                uid = hashlib.sha1(f"{seccion}|{nombre}|{fecha}".encode()).hexdigest()[:16]
                salida.append(Anexo(uid, f"[{seccion}] {nombre}", proc_url, fecha))
            sig = p.get_by_role("button", name=re.compile(r"siguiente|next", re.I))
            if sig.count() and sig.first.is_enabled():
                sig.first.click()
                self._settle()
            else:
                break
        return salida

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
            docs: list[Anexo] = []
            for sec in SECTIONS:
                docs += self._leer_seccion(sec, proc_url)
            if self.debug:
                self._dump(f"final_{numero}")
            time.sleep(random.uniform(0.3, 0.8))
            return docs
        except NoEncontrado:
            raise
        except Exception:
            self._dump(f"error_{numero}")
            raise
