"""Cliente minimo de TickTick (Open API). Etapa 2a: solo LECTURA para comprobar que el token sirve.
Nunca imprime el token; solo codigos de estado y nombres de listas."""
import os
import sys

import requests

BASE = "https://api.ticktick.com/open/v1"


def _h():
    return {"Authorization": f"Bearer {os.environ['TICKTICK_TOKEN'].strip()}"}


def proyectos():
    r = requests.get(f"{BASE}/project", headers=_h(), timeout=30)
    return r.status_code, (r.json() if r.ok else r.text[:200])


def sonda():
    if not os.environ.get("TICKTICK_TOKEN"):
        print("Falta el secreto TICKTICK_TOKEN")
        return 1
    st, data = proyectos()
    print(f"GET /project -> HTTP {st}")
    if st != 200:
        print("respuesta:", data)
        return 1
    for p in data:
        print(f"  lista: {p.get('name')!r} | grupo(carpeta): {p.get('groupId')} | id: {p.get('id')} | cerrada: {p.get('closed')}")
    return 0


if __name__ == "__main__":
    sys.exit(sonda())
