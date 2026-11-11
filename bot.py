#!/usr/bin/env python3
"""
3 Cargadores — Bot DCA simulado para BTC/USDT:USDT (BingX)
==========================================================

Proyecto abierto para compartir proyectos y conocimientos técnicos. Demuestra:

* Consumo de la API pública de BingX mediante ``ccxt`` (solo lectura).
* Una estrategia DCA ("Dollar Cost Averaging") con gestión de capital por
  "balas" repartidas en 3 cargadores y defensa de margen aislado.
* Persistencia de estado en un archivo JSON (``estado_bot.json``) sin base de datos.
* Reportes visuales: resumen estilizado en terminal y ``reporte.html`` autocontenido.

El modo simulación (``DRY_RUN``) está **hardcodeado**: el código no contiene
ninguna llamada que envíe órdenes al exchange. Cada ejecución equivale a un
"día" de la estrategia.

Uso::

    python bot.py                 # precio real de BingX (API pública)
    python bot.py --precio 68000  # precio manual (modo offline / escenarios)
    python bot.py --reset         # reinicia la simulación
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import ccxt
from dotenv import load_dotenv

# =========================================================================== #
# Constantes
# =========================================================================== #
DRY_RUN: bool = True  # Hardcodeado: este proyecto NUNCA envía órdenes reales.

SYMBOL = "BTC/USDT:USDT"            # Futuro perpetuo lineal (swap) en BingX
APALANCAMIENTO = 5                  # 5X, margen aislado, solo LONG
BALAS_POR_CARGADOR = 30
CARGADORES = ("cargador_1", "cargador_2", "cargador_3")
BALAS_TOTALES = BALAS_POR_CARGADOR * len(CARGADORES)
BALAS_INICIO_CICLO = 2
TAKE_PROFIT_PCT = 15.0              # PnL % sobre el margen
UMBRAL_LIQUIDACION_PCT = 6.0        # distancia (%) al precio de liquidación
BALAS_ANTI_LIQUIDACION = 5
TASA_MANTENIMIENTO = 0.004          # margen de mantenimiento aproximado (0.4 %)

VERSION_ESTADO = 2
MAX_HISTORIAL = 500
MAX_SERIE = 365

BASE_DIR = Path(__file__).resolve().parent
RUTA_ESTADO = BASE_DIR / "estado_bot.json"
RUTA_REPORTE = BASE_DIR / "reporte.html"

#: Metadatos de presentación de cada tipo de evento: (etiqueta, categoría CSS).
EVENTOS: dict[str, tuple[str, str]] = {
    "INICIO_CICLO": ("Inicio de ciclo", "compra"),
    "RECARGA_SUAVE": ("Recarga suave", "compra"),
    "RECARGA_MEDIA": ("Recarga media", "compra"),
    "RECARGA_FUERTE": ("Recarga fuerte", "compra"),
    "DEFENSA_CRITICA": ("Defensa crítica", "defensa"),
    "DEFENSA_TERMINAL": ("Defensa terminal", "defensa"),
    "ANTI_LIQUIDACION": ("Anti-liquidación", "defensa"),
    "TAKE_PROFIT": ("Take Profit", "ganancia"),
    "LIQUIDACION": ("Liquidación", "perdida"),
    "SIN_BALAS": ("Sin balas (HOLD)", "neutro"),
}

log = logging.getLogger("3cargadores")


# =========================================================================== #
# Configuración
# =========================================================================== #
@dataclass(frozen=True)
class Config:
    """Parámetros de ejecución leídos de ``.env`` (todos opcionales).

    Attributes:
        valor_bala: Valor en USDT de cada bala (margen, antes del apalancamiento).
        api_key: Clave pública de BingX (opcional, solo para lectura de saldo).
        secret: Clave secreta de BingX (opcional).
    """

    valor_bala: float = 10.0
    api_key: str = ""
    secret: str = ""

    @property
    def tiene_claves(self) -> bool:
        """``True`` si hay credenciales para endpoints privados de solo lectura."""
        return bool(self.api_key and self.secret)


def cargar_config() -> Config:
    """Lee la configuración desde variables de entorno / ``.env``.

    ``DRY_RUN`` se lee únicamente para advertir: si alguien lo pone en ``False``
    se ignora, porque el proyecto opera exclusivamente en simulación.

    Raises:
        SystemExit: si ``VALOR_BALA_USDT`` no es un número positivo.
    """
    load_dotenv(BASE_DIR / ".env")
    try:
        valor_bala = float(os.getenv("VALOR_BALA_USDT", "10"))
    except ValueError:
        raise SystemExit("VALOR_BALA_USDT debe ser numérico.") from None
    if valor_bala <= 0:
        raise SystemExit("VALOR_BALA_USDT debe ser mayor que 0.")

    if os.getenv("DRY_RUN", "True").strip().lower() in ("false", "0", "no"):
        log.warning("DRY_RUN=False ignorado: este proyecto opera exclusivamente en modo simulación.")

    return Config(
        valor_bala=valor_bala,
        api_key=os.getenv("BINGX_API_KEY", "").strip(),
        secret=os.getenv("BINGX_SECRET_KEY", "").strip(),
    )


# =========================================================================== #
# Estado (estado_bot.json)
# =========================================================================== #
def ahora_iso() -> str:
    """Fecha y hora actual en UTC, formato ISO-8601 compacto."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cargadores_llenos() -> dict[str, int]:
    """Los 3 cargadores con 30 balas cada uno."""
    return {nombre: BALAS_POR_CARGADOR for nombre in CARGADORES}


def posicion_vacia() -> dict[str, float]:
    """Posición simulada sin contratos."""
    return {"contratos": 0.0, "precio_entrada": 0.0, "margen": 0.0}


def nuevo_ciclo(numero: int) -> dict[str, Any]:
    """Contadores de un ciclo nuevo (un ciclo termina en Take Profit o liquidación)."""
    return {"numero": numero, "inicio": None, "balas_usadas": 0,
            "usdt_compras": 0.0, "usdt_margen": 0.0}


def estado_inicial() -> dict[str, Any]:
    """Estructura completa de un estado nuevo."""
    return {
        "version": VERSION_ESTADO,
        "modo": "SIMULACION",
        "creado": ahora_iso(),
        "cargadores": cargadores_llenos(),
        "posicion": posicion_vacia(),
        "ciclo": nuevo_ciclo(1),
        "estadisticas": {"ejecuciones": 0, "ciclos_cerrados": 0,
                         "liquidaciones": 0, "pnl_realizado_usdt": 0.0},
        "historial": [],
        "serie": [],
    }


def cargar_estado(ruta: Path) -> dict[str, Any]:
    """Carga el estado desde disco o crea uno nuevo.

    Si el archivo es de un esquema anterior se respalda como ``.bak`` y se
    empieza de cero. Si el JSON está corrupto se aborta para no perder datos.
    """
    if not ruta.exists():
        log.info("No existe %s: se inicia una simulación nueva.", ruta.name)
        return estado_inicial()
    try:
        estado = json.loads(ruta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"No se pudo leer {ruta.name}: {exc}. Revísalo o usa --reset.") from None

    if estado.get("version") != VERSION_ESTADO:
        respaldo = ruta.with_suffix(".json.bak")
        ruta.replace(respaldo)
        log.warning("Estado con esquema antiguo respaldado en %s. Se inicia uno nuevo.", respaldo.name)
        return estado_inicial()
    return estado


def guardar_estado(estado: dict[str, Any], ruta: Path) -> None:
    """Guarda el estado de forma atómica (archivo temporal + reemplazo)."""
    estado["historial"] = estado["historial"][-MAX_HISTORIAL:]
    estado["serie"] = estado["serie"][-MAX_SERIE:]
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(ruta)


# =========================================================================== #
# Modelo de posición
# =========================================================================== #
@dataclass(frozen=True)
class Posicion:
    """Foto de la posición LONG simulada valuada a un precio dado."""

    contratos: float
    precio_entrada: float
    margen: float
    precio: float

    @property
    def nocional(self) -> float:
        """Valor de la posición en USDT al precio actual."""
        return self.contratos * self.precio

    @property
    def pnl_usdt(self) -> float:
        """PnL no realizado en USDT: (P - E) · Q."""
        return (self.precio - self.precio_entrada) * self.contratos

    @property
    def pnl_pct(self) -> float:
        """PnL no realizado como % del margen aislado (ROE)."""
        return self.pnl_usdt / self.margen * 100 if self.margen else 0.0

    @property
    def precio_liquidacion(self) -> float:
        """Precio de liquidación aproximado de un LONG aislado.

        Se liquida cuando ``margen + (P - E)·Q = mm · P · Q``, de donde
        ``P_liq = (E·Q - margen) / (Q · (1 - mm))``.
        """
        liq = (self.precio_entrada * self.contratos - self.margen) / (
            self.contratos * (1 - TASA_MANTENIMIENTO))
        return max(liq, 0.0)

    @property
    def distancia_liquidacion_pct(self) -> float:
        """Caída porcentual del precio necesaria para llegar a la liquidación."""
        return (self.precio - self.precio_liquidacion) / self.precio * 100


def leer_posicion(estado: dict[str, Any], precio: float) -> Posicion | None:
    """Devuelve la posición simulada valuada a ``precio`` o ``None`` si no hay."""
    p = estado["posicion"]
    if p["contratos"] <= 0:
        return None
    return Posicion(p["contratos"], p["precio_entrada"], p["margen"], precio)


# =========================================================================== #
# Gestión de balas
# =========================================================================== #
def balas_restantes(estado: dict[str, Any]) -> int:
    """Total de balas sin usar en los 3 cargadores."""
    return sum(estado["cargadores"].values())


def balas_operativas(estado: dict[str, Any]) -> int:
    """Balas disponibles para compras: cargador_1 + cargador_2."""
    c = estado["cargadores"]
    return c["cargador_1"] + c["cargador_2"]


def consumir_balas(estado: dict[str, Any], n: int, fuente: str) -> dict[str, int]:
    """Descuenta hasta ``n`` balas y devuelve de qué cargador salió cada una.

    Args:
        estado: Estado del bot (se modifica).
        n: Balas solicitadas.
        fuente: ``"operativa"`` (cargador_1 y, al agotarse, cargador_2) o el
            nombre de un cargador concreto (p. ej. ``"cargador_3"``).

    Returns:
        Mapa ``{cargador: balas_consumidas}``.
    """
    orden = ("cargador_1", "cargador_2") if fuente == "operativa" else (fuente,)
    plan: dict[str, int] = {}
    restante = n
    for nombre in orden:
        usar = min(restante, estado["cargadores"][nombre])
        if usar:
            estado["cargadores"][nombre] -= usar
            plan[nombre] = usar
            restante -= usar
    estado["ciclo"]["balas_usadas"] += n - restante
    return plan


# =========================================================================== #
# Reglas de la estrategia (funciones puras)
# =========================================================================== #
@dataclass(frozen=True)
class Accion:
    """Acción a ejecutar: cuántas balas van a compra y cuántas a margen."""

    nombre: str
    balas_compra: int
    balas_margen: int
    fuente: str  # "operativa" | "cargador_3"

    @property
    def total(self) -> int:
        """Balas totales que consume la acción."""
        return self.balas_compra + self.balas_margen


def decidir_accion(pnl_pct: float) -> Accion | None:
    """Regla diaria con posición abierta según el PnL % sobre el margen.

    ============================  =================================================
    Tramo de PnL                  Acción
    ============================  =================================================
    PnL >= +15 %                  ``None`` → Take Profit (se gestiona aparte)
    PnL > -5 %                    1 bala de compra
    -15 % < PnL <= -5 %           2 balas de compra
    -40 % < PnL <= -15 %          3 balas de compra
    -60 % < PnL <= -40 %          2 balas de compra + 2 balas de margen
    PnL <= -60 %                  4 balas de margen desde ``cargador_3``
    ============================  =================================================
    """
    if pnl_pct >= TAKE_PROFIT_PCT:
        return None
    if pnl_pct > -5.0:
        return Accion("RECARGA_SUAVE", 1, 0, "operativa")
    if pnl_pct > -15.0:
        return Accion("RECARGA_MEDIA", 2, 0, "operativa")
    if pnl_pct > -40.0:
        return Accion("RECARGA_FUERTE", 3, 0, "operativa")
    if pnl_pct > -60.0:
        return Accion("DEFENSA_CRITICA", 2, 2, "operativa")
    return Accion("DEFENSA_TERMINAL", 0, 4, "cargador_3")


def accion_anti_liquidacion() -> Accion:
    """5 balas (cargador_1 → cargador_2) inyectadas 100 % como margen aislado."""
    return Accion("ANTI_LIQUIDACION", 0, BALAS_ANTI_LIQUIDACION, "operativa")


# =========================================================================== #
# Motor de simulación (sin red: recibe el precio como parámetro)
# =========================================================================== #
def _fmt_plan(plan: dict[str, int]) -> str:
    """``{'cargador_1': 3}`` → ``'C1×3'``."""
    return ", ".join(f"C{k[-1]}×{v}" for k, v in plan.items())


def registrar(estado: dict[str, Any], evento: str, precio: float, pos: Posicion | None,
              detalle: str, **datos: Any) -> dict[str, Any]:
    """Agrega un evento al historial y lo devuelve."""
    entrada = {
        "fecha": ahora_iso(),
        "ejecucion": estado["estadisticas"]["ejecuciones"],
        "ciclo": estado["ciclo"]["numero"],
        "evento": evento,
        "precio": round(precio, 2),
        "pnl_pct": round(pos.pnl_pct, 2) if pos else None,
        "detalle": detalle,
        **datos,
    }
    estado["historial"].append(entrada)
    return entrada


def simular_compra(estado: dict[str, Any], margen_usdt: float, precio: float) -> float:
    """Compra a mercado simulada: suma contratos y recalcula el precio promedio.

    Returns:
        Contratos (BTC) comprados: ``margen · apalancamiento / precio``.
    """
    p = estado["posicion"]
    contratos = margen_usdt * APALANCAMIENTO / precio
    total = p["contratos"] + contratos
    p["precio_entrada"] = (p["precio_entrada"] * p["contratos"] + precio * contratos) / total
    p["contratos"] = round(total, 8)
    p["margen"] = round(p["margen"] + margen_usdt, 8)
    estado["ciclo"]["usdt_compras"] += margen_usdt
    return contratos


def simular_margen(estado: dict[str, Any], usdt: float) -> None:
    """Equivalente simulado de ``exchange.add_margin``: más colateral, mismos contratos."""
    estado["posicion"]["margen"] = round(estado["posicion"]["margen"] + usdt, 8)
    estado["ciclo"]["usdt_margen"] += usdt


def aplicar_accion(estado: dict[str, Any], accion: Accion, precio: float,
                   cfg: Config) -> dict[str, Any]:
    """Ejecuta una :class:`Accion` sobre la posición simulada.

    Si no hay balas suficientes usa las que queden, priorizando el margen
    (defensa) sobre la compra. Si no queda ninguna, registra ``SIN_BALAS``.
    """
    pos = leer_posicion(estado, precio)
    disponible = (estado["cargadores"]["cargador_3"] if accion.fuente == "cargador_3"
                  else balas_operativas(estado))
    if disponible <= 0:
        return registrar(estado, "SIN_BALAS", precio, pos,
                         f"Sin balas para {accion.nombre} · se mantiene la posición",
                         accion=accion.nombre)

    total = min(accion.total, disponible)
    balas_margen = min(accion.balas_margen, total)
    balas_compra = total - balas_margen
    plan = consumir_balas(estado, total, accion.fuente)

    partes: list[str] = []
    contratos = 0.0
    if balas_compra:
        usdt = balas_compra * cfg.valor_bala
        contratos = simular_compra(estado, usdt, precio)
        partes.append(f"compra {balas_compra} bala(s) ({usdt:.2f} USDT) → {contratos:.5f} BTC")
    if balas_margen:
        usdt = balas_margen * cfg.valor_bala
        simular_margen(estado, usdt)
        partes.append(f"margen +{balas_margen} bala(s) ({usdt:.2f} USDT)")

    detalle = " + ".join(partes) + f" · origen {_fmt_plan(plan)}"
    if accion.nombre == "ANTI_LIQUIDACION" and pos:
        detalle = f"Precio a {pos.distancia_liquidacion_pct:.2f}% de la liquidación → " + detalle
    if total < accion.total:
        detalle += f" · parcial ({total}/{accion.total})"

    return registrar(estado, accion.nombre, precio, pos, detalle,
                     balas_compra=balas_compra, balas_margen=balas_margen,
                     contratos=round(contratos, 8), origen=plan)


def cerrar_ciclo(estado: dict[str, Any]) -> None:
    """Recarga los 3 cargadores, vacía la posición y abre un ciclo nuevo."""
    estado["cargadores"] = cargadores_llenos()
    estado["posicion"] = posicion_vacia()
    estado["ciclo"] = nuevo_ciclo(estado["ciclo"]["numero"] + 1)


def iniciar_ciclo(estado: dict[str, Any], precio: float, cfg: Config) -> dict[str, Any]:
    """Abre la posición LONG con 2 balas del ``cargador_1``."""
    if estado["cargadores"]["cargador_1"] < BALAS_INICIO_CICLO:
        return registrar(estado, "SIN_BALAS", precio, None, "cargador_1 sin balas para abrir ciclo")
    plan = consumir_balas(estado, BALAS_INICIO_CICLO, "cargador_1")
    usdt = BALAS_INICIO_CICLO * cfg.valor_bala
    contratos = simular_compra(estado, usdt, precio)
    estado["ciclo"]["inicio"] = ahora_iso()
    return registrar(estado, "INICIO_CICLO", precio, None,
                     f"Apertura LONG {APALANCAMIENTO}X: {BALAS_INICIO_CICLO} balas "
                     f"({usdt:.2f} USDT) → {contratos:.5f} BTC",
                     balas_compra=BALAS_INICIO_CICLO, balas_margen=0,
                     contratos=round(contratos, 8), origen=plan)


def take_profit(estado: dict[str, Any], pos: Posicion) -> dict[str, Any]:
    """Cierra el 100 % de la posición (``reduceOnly``) y recarga los cargadores."""
    pnl = pos.pnl_usdt
    est = estado["estadisticas"]
    est["ciclos_cerrados"] += 1
    est["pnl_realizado_usdt"] = round(est["pnl_realizado_usdt"] + pnl, 4)
    evento = registrar(estado, "TAKE_PROFIT", pos.precio, pos,
                       f"Cierre 100% de {pos.contratos:.5f} BTC · PnL {pnl:+.2f} USDT · "
                       f"{estado['ciclo']['balas_usadas']} balas usadas · cargadores recargados",
                       pnl_usdt=round(pnl, 4))
    cerrar_ciclo(estado)
    return evento


def liquidacion(estado: dict[str, Any], pos: Posicion) -> dict[str, Any]:
    """Registra la liquidación simulada: se pierde todo el margen aislado."""
    perdida = -pos.margen
    est = estado["estadisticas"]
    est["liquidaciones"] += 1
    est["ciclos_cerrados"] += 1
    est["pnl_realizado_usdt"] = round(est["pnl_realizado_usdt"] + perdida, 4)
    evento = registrar(estado, "LIQUIDACION", pos.precio, pos,
                       f"Precio {pos.precio:,.2f} <= liquidación {pos.precio_liquidacion:,.2f} · "
                       f"pérdida {perdida:.2f} USDT",
                       pnl_usdt=round(perdida, 4))
    cerrar_ciclo(estado)
    return evento


def agregar_snapshot(estado: dict[str, Any], precio: float) -> None:
    """Guarda un punto de la serie temporal (usado por los gráficos del reporte)."""
    pos = leer_posicion(estado, precio)
    estado["serie"].append({
        "ejecucion": estado["estadisticas"]["ejecuciones"],
        "fecha": ahora_iso(),
        "precio": round(precio, 2),
        "pnl_pct": round(pos.pnl_pct, 2) if pos else None,
        "precio_entrada": round(pos.precio_entrada, 2) if pos else None,
        "precio_liquidacion": round(pos.precio_liquidacion, 2) if pos else None,
        "balas_restantes": balas_restantes(estado),
    })


def ejecutar_dia(estado: dict[str, Any], precio: float, cfg: Config) -> list[dict[str, Any]]:
    """Ejecuta un "día" completo de la estrategia al precio dado.

    Orden de evaluación:

    1. Sin posición → inicio de ciclo (2 balas).
    2. Precio <= liquidación → liquidación simulada.
    3. PnL >= 15 % → Take Profit.
    4. Distancia a liquidación <= 6 % → anti-liquidación (5 balas de margen).
    5. Acción diaria según el tramo de PnL (recalculado tras el paso 4).

    Returns:
        Lista de eventos registrados en esta ejecución.
    """
    if precio <= 0:
        raise ValueError("El precio debe ser positivo.")
    estado["estadisticas"]["ejecuciones"] += 1
    eventos: list[dict[str, Any]] = []
    pos = leer_posicion(estado, precio)

    if pos is None:
        eventos.append(iniciar_ciclo(estado, precio, cfg))
    elif precio <= pos.precio_liquidacion:
        eventos.append(liquidacion(estado, pos))
    elif pos.pnl_pct >= TAKE_PROFIT_PCT:
        eventos.append(take_profit(estado, pos))
    else:
        if pos.distancia_liquidacion_pct <= UMBRAL_LIQUIDACION_PCT:
            eventos.append(aplicar_accion(estado, accion_anti_liquidacion(), precio, cfg))
            pos = leer_posicion(estado, precio)
        accion = decidir_accion(pos.pnl_pct) if pos else None
        if accion:
            eventos.append(aplicar_accion(estado, accion, precio, cfg))

    agregar_snapshot(estado, precio)
    return eventos


# =========================================================================== #
# Conexión con BingX (solo lectura) y Telemetría de API
# =========================================================================== #
def con_reintentos(fn: Callable[..., Any], *args: Any, intentos: int = 3,
                   espera: float = 2.0) -> Any:
    """Ejecuta ``fn`` reintentando con backoff exponencial ante errores de red."""
    for i in range(1, intentos + 1):
        try:
            return fn(*args)
        except ccxt.NetworkError as exc:
            if i == intentos:
                raise
            log.warning("Error de red (%s). Reintento %d/%d en %.0fs...",
                        type(exc).__name__, i, intentos - 1, espera)
            time.sleep(espera)
            espera *= 2
    raise RuntimeError("inalcanzable")


def crear_exchange(cfg: Config) -> ccxt.Exchange:
    """Instancia el cliente ``ccxt.bingx`` para futuros perpetuos (swap)."""
    params: dict[str, Any] = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
    if cfg.tiene_claves:
        params.update(apiKey=cfg.api_key, secret=cfg.secret)
    return ccxt.bingx(params)


def consultar_telemetria_bingx(exchange: ccxt.Exchange) -> dict[str, Any]:
    """Consulta múltiples endpoints públicos de BingX y mide la latencia de cada uno.

    Endpoints consumidos:
        1. ``fetch_ticker(SYMBOL)``: Precio actual, máximo/mínimo 24h, cambio % 24h y volumen.
        2. ``fetch_funding_rate(SYMBOL)``: Tasa de financiación e índice/marca del perpetuo.
        3. ``fetch_ohlcv(SYMBOL, '1h', limit=24)``: Últimas 24 velas horarias reales.

    Returns:
        Diccionario serializable con precios, métricas 24h, velas OHLCV y registro de
        llamadas HTTP (endpoint, estado, latencia en ms).
    """
    llamadas: list[dict[str, Any]] = []

    # 1. Endpoint Ticker 24h
    t0 = time.perf_counter()
    ticker = con_reintentos(exchange.fetch_ticker, SYMBOL)
    ms_ticker = round((time.perf_counter() - t0) * 1000, 1)
    precio = ticker.get("last") or ticker.get("close")
    if not precio:
        raise ccxt.ExchangeError("El ticker no devolvió precio.")
    llamadas.append({
        "endpoint": "GET /openApi/swap/v2/quote/ticker",
        "metodo": f"fetch_ticker('{SYMBOL}')",
        "estado": "200 OK",
        "latencia_ms": ms_ticker,
    })

    # 2. Endpoint Funding Rate & Mark Price
    funding_rate: float | None = None
    mark_price: float | None = None
    try:
        t0 = time.perf_counter()
        fr = con_reintentos(exchange.fetch_funding_rate, SYMBOL)
        ms_fr = round((time.perf_counter() - t0) * 1000, 1)
        if fr.get("fundingRate") is not None:
            funding_rate = round(float(fr["fundingRate"]) * 100, 4)  # en %
        if fr.get("markPrice") is not None:
            mark_price = round(float(fr["markPrice"]), 2)
        llamadas.append({
            "endpoint": "GET /openApi/swap/v2/quote/premiumIndex",
            "metodo": f"fetch_funding_rate('{SYMBOL}')",
            "estado": "200 OK",
            "latencia_ms": ms_fr,
        })
    except ccxt.BaseError as exc:
        log.warning("No se pudo obtener funding rate: %s", exc)

    # 3. Endpoint Velas Históricas (OHLCV 1h - últimas 24h)
    velas_24h: list[dict[str, Any]] = []
    try:
        t0 = time.perf_counter()
        ohlcv = con_reintentos(exchange.fetch_ohlcv, SYMBOL, "1h", None, 24)
        ms_ohlcv = round((time.perf_counter() - t0) * 1000, 1)
        for v in ohlcv[-24:]:
            ts_str = datetime.fromtimestamp(v[0] / 1000, tz=timezone.utc).strftime("%H:%M")
            velas_24h.append({
                "hora": ts_str,
                "open": round(float(v[1]), 2),
                "high": round(float(v[2]), 2),
                "low": round(float(v[3]), 2),
                "close": round(float(v[4]), 2),
                "vol": round(float(v[5]), 2),
            })
        llamadas.append({
            "endpoint": "GET /openApi/swap/v3/quote/klines",
            "metodo": f"fetch_ohlcv('{SYMBOL}', '1h', limit=24)",
            "estado": "200 OK",
            "latencia_ms": ms_ohlcv,
        })
    except ccxt.BaseError as exc:
        log.warning("No se pudo obtener OHLCV 24h: %s", exc)

    latencia_total = round(sum(c["latencia_ms"] for c in llamadas), 1)
    return {
        "modo_fuente": "BingX REST API (en vivo)",
        "consultado_utc": ahora_iso(),
        "simbolo": SYMBOL,
        "precio": round(float(precio), 2),
        "mark_price": mark_price,
        "cambio_24h_pct": round(float(ticker.get("percentage") or 0.0), 2),
        "max_24h": round(float(ticker.get("high") or precio), 2),
        "min_24h": round(float(ticker.get("low") or precio), 2),
        "volumen_24h_usdt": round(float(ticker.get("quoteVolume") or 0.0), 0),
        "funding_rate_pct": funding_rate,
        "latencia_total_ms": latencia_total,
        "llamadas": llamadas,
        "velas_1h": velas_24h,
    }


def obtener_saldo(exchange: ccxt.Exchange, cfg: Config) -> float | None:
    """Saldo libre en USDT (endpoint privado de solo lectura). ``None`` si no aplica."""
    if not cfg.tiene_claves:
        return None
    try:
        balance = con_reintentos(exchange.fetch_balance)
        return float((balance.get("USDT") or {}).get("free") or 0.0)
    except ccxt.BaseError as exc:
        log.warning("No se pudo leer el saldo (se continúa sin él): %s", exc)
        return None


# =========================================================================== #
# Reporte en terminal
# =========================================================================== #
class Estilo:
    """Colores ANSI (24-bit) con desactivación automática fuera de una TTY o con NO_COLOR."""

    activo = sys.stdout.isatty() and not os.getenv("NO_COLOR")

    @classmethod
    def _c(cls, codigo: str, texto: str) -> str:
        return f"\033[{codigo}m{texto}\033[0m" if cls.activo else texto

    @classmethod
    def naranja(cls, t: str) -> str:
        return cls._c("1;38;2;242;101;34", t)

    @classmethod
    def cyan(cls, t: str) -> str:
        return cls._c("38;2;34;211;238", t)

    @classmethod
    def verde(cls, t: str) -> str:
        return cls._c("38;2;52;211;153", t)

    @classmethod
    def rojo(cls, t: str) -> str:
        return cls._c("38;2;248;113;113", t)

    @classmethod
    def tenue(cls, t: str) -> str:
        return cls._c("38;2;143;179;217", t)

    @classmethod
    def negrita(cls, t: str) -> str:
        return cls._c("1", t)


def barra(valor: float, total: float, ancho: int = 20) -> str:
    """Barra de progreso en texto: ``██████░░░░``."""
    llenos = round(max(0.0, min(1.0, valor / total)) * ancho) if total else 0
    return "█" * llenos + "░" * (ancho - llenos)


def _color_pnl(valor: float, texto: str) -> str:
    return Estilo.verde(texto) if valor >= 0 else Estilo.rojo(texto)


def imprimir_reporte(estado: dict[str, Any], precio: float, eventos: Sequence[dict[str, Any]],
                     fuente_precio: str, saldo: float | None) -> None:
    """Imprime el resumen estilizado de la ejecución (pensado para capturas)."""
    E = Estilo
    ancho = 66
    titulo = "3 CARGADORES · DCA BOT · BingX BTC/USDT:USDT · DEMO"
    est = estado["estadisticas"]

    print()
    print(E.naranja("╭" + "─" * (ancho - 2) + "╮"))
    print(E.naranja("│" + titulo.center(ancho - 2) + "│"))
    print(E.naranja("╰" + "─" * (ancho - 2) + "╯"))
    print(E.tenue(f"  Ejecución #{est['ejecuciones']} · Ciclo #{estado['ciclo']['numero']} · "
                  f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · precio: {fuente_precio}"))

    print("\n" + E.cyan("  MERCADO & TELEMETRÍA API (BingX REST)"))
    print(f"  ├─ Precio BTC ............ {E.negrita(f'{precio:,.2f} USDT')}")
    tele = estado.get("telemetria_api")
    if tele and fuente_precio == "BingX API pública":
        c24 = tele.get("cambio_24h_pct", 0.0)
        rango = f"{tele.get('min_24h', 0):,.1f} – {tele.get('max_24h', 0):,.1f} USDT"
        vol_m = (tele.get("volumen_24h_usdt") or 0) / 1_000_000
        fr = tele.get("funding_rate_pct")
        fr_str = f"{fr:+.4f}%" if fr is not None else "n/d"
        print(f"  ├─ Rango 24h / Cambio .... {rango} ({_color_pnl(c24, f'{c24:+.2f}%')})")
        print(f"  ├─ Volumen 24h / Funding . {vol_m:,.1f}M USDT · Funding: {fr_str}")
        for idx, ll in enumerate(tele.get("llamadas", [])):
            pref = "└─" if idx == len(tele["llamadas"]) - 1 and saldo is None else "├─"
            est_ok = E.verde(ll["estado"])
            print(f"  {pref} {ll['endpoint']:<36} {est_ok} ({ll['latencia_ms']} ms)")
    if saldo is not None:
        print(f"  └─ Saldo real (lectura) .. {saldo:,.2f} USDT")

    print("\n" + E.cyan(f"  POSICIÓN SIMULADA · LONG {APALANCAMIENTO}X aislado"))
    pos = leer_posicion(estado, precio)
    if pos:
        pnl = f"{pos.pnl_usdt:+,.2f} USDT ({pos.pnl_pct:+.2f}%)"
        dist = pos.distancia_liquidacion_pct
        liq = f"{pos.precio_liquidacion:,.2f} (a {dist:.2f}%)"
        print(f"  ├─ Contratos ............. {pos.contratos:.5f} BTC  ({pos.nocional:,.2f} USDT)")
        print(f"  ├─ Precio de entrada ..... {pos.precio_entrada:,.2f}")
        print(f"  ├─ Margen aislado ........ {pos.margen:,.2f} USDT")
        print(f"  ├─ PnL no realizado ...... {_color_pnl(pos.pnl_usdt, pnl)}")
        print(f"  ├─ Progreso a TP (+15%) .. [{barra(max(pos.pnl_pct, 0), TAKE_PROFIT_PCT, 15)}]")
        print(f"  └─ Liquidación ........... "
              f"{E.rojo(liq) if dist <= UMBRAL_LIQUIDACION_PCT else liq}")
    else:
        print(E.tenue("  └─ Sin posición abierta (se abrirá en la próxima ejecución)"))

    print("\n" + E.cyan("  ACCIONES DE HOY"))
    for ev in eventos:
        etiqueta, categoria = EVENTOS.get(ev["evento"], (ev["evento"], "neutro"))
        color = {"compra": E.cyan, "defensa": E.naranja, "ganancia": E.verde,
                 "perdida": E.rojo}.get(categoria, E.tenue)
        print(f"  ▸ {color(etiqueta.ljust(18))} {ev['detalle']}")

    print("\n" + E.cyan("  CARGADORES"))
    nombres = {"cargador_1": "C1 Operativo", "cargador_2": "C2 Táctico", "cargador_3": "C3 Extremo"}
    for clave, nombre in nombres.items():
        n = estado["cargadores"][clave]
        print(f"  {nombre:<13} [{barra(n, BALAS_POR_CARGADOR)}] {n:>2}/{BALAS_POR_CARGADOR}")
    total = balas_restantes(estado)
    print(f"  {E.negrita('TOTAL'):<13} [{E.naranja(barra(total, BALAS_TOTALES))}] "
          f"{E.negrita(f'{total}/{BALAS_TOTALES} Balas Restantes')}")
    if not E.activo:
        pass

    pnl_r = est["pnl_realizado_usdt"]
    print("\n" + E.cyan("  ESTADÍSTICAS"))
    print(f"  Ciclos cerrados {est['ciclos_cerrados']} · Liquidaciones {est['liquidaciones']} · "
          f"PnL realizado {_color_pnl(pnl_r, f'{pnl_r:+,.2f} USDT')}")
    print()


# =========================================================================== #
# Reporte HTML
# =========================================================================== #
def svg_lineas(etiquetas: Sequence[Any],
               series: Sequence[tuple[str, Sequence[float | None], str, bool]],
               referencias: Sequence[tuple[float, str, str]] = (),
               ancho: int = 760, alto: int = 260, sufijo: str = "") -> str:
    """Genera un gráfico de líneas SVG autocontenido (sin JavaScript ni librerías).

    Args:
        etiquetas: Etiquetas del eje X (una por punto).
        series: Tuplas ``(nombre, valores, color, punteada)``. ``None`` = sin dato.
        referencias: Líneas horizontales ``(valor, etiqueta, color)``.
        sufijo: Texto añadido a los valores del eje Y (p. ej. ``"%"``).
    """
    valores = [v for _, vs, _, _ in series for v in vs if v is not None]
    if not valores:
        return '<p class="vacio">Aún no hay datos suficientes para graficar.</p>'
    todos = valores + [r[0] for r in referencias]
    vmin, vmax = min(todos), max(todos)
    if vmin == vmax:
        vmin, vmax = vmin - 1, vmax + 1
    margen_v = (vmax - vmin) * 0.08
    vmin, vmax = vmin - margen_v, vmax + margen_v
    izq, der, sup, inf = 70, 18, 14, 30
    n = len(etiquetas)

    def x(i: int) -> float:
        return izq + (i * (ancho - izq - der) / (n - 1) if n > 1 else (ancho - izq - der) / 2)

    def y(v: float) -> float:
        return sup + (vmax - v) * (alto - sup - inf) / (vmax - vmin)

    s = [f'<svg viewBox="0 0 {ancho} {alto}" class="grafico" role="img">']
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        s.append(f'<line x1="{izq}" x2="{ancho - der}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                 f'<text x="{izq - 8}" y="{y(v) + 4:.1f}" class="eje" text-anchor="end">'
                 f'{v:,.1f}{sufijo}</text>')
    for valor, texto, color in referencias:
        s.append(f'<line x1="{izq}" x2="{ancho - der}" y1="{y(valor):.1f}" y2="{y(valor):.1f}" '
                 f'stroke="{color}" stroke-dasharray="4 4" stroke-width="1" opacity=".7"/>'
                 f'<text x="{ancho - der - 4}" y="{y(valor) - 5:.1f}" fill="{color}" '
                 f'class="eje" text-anchor="end">{html.escape(texto)}</text>')
    paso = max(1, n // 10)
    for i in range(0, n, paso):
        s.append(f'<text x="{x(i):.1f}" y="{alto - 8}" class="eje" text-anchor="middle">'
                 f'{html.escape(str(etiquetas[i]))}</text>')
    for nombre, vs, color, punteada in series:
        pts = [(x(i), y(v), v) for i, v in enumerate(vs) if v is not None]
        if not pts:
            continue
        dash = ' stroke-dasharray="6 5"' if punteada else ""
        s.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.5"{dash} '
                 f'stroke-linejoin="round" points="{" ".join(f"{a:.1f},{b:.1f}" for a, b, _ in pts)}"/>')
        if not punteada:
            for a, b, v in pts:
                s.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.5" fill="{color}">'
                         f'<title>{html.escape(nombre)}: {v:,.2f}{sufijo}</title></circle>')
    s.append("</svg>")
    return "".join(s)


def _leyenda(items: Sequence[tuple[str, str]]) -> str:
    return '<div class="leyenda">' + "".join(
        f'<span><i style="background:{c}"></i>{html.escape(t)}</span>' for t, c in items) + "</div>"


def svg_velas(velas: Sequence[dict[str, Any]], ancho: int = 760, alto: int = 260) -> str:
    """Genera un gráfico SVG de velas japonesas (OHLC) de las últimas 24 horas en BingX."""
    if not velas:
        return '<p class="vacio">Ejecuta sin --precio para descargar las 24 velas horarias desde la API de BingX.</p>'
    vmin = min(v["low"] for v in velas)
    vmax = max(v["high"] for v in velas)
    if vmin == vmax:
        vmin, vmax = vmin - 1, vmax + 1
    pad = (vmax - vmin) * 0.08
    vmin, vmax = vmin - pad, vmax + pad
    izq, der, sup, inf = 70, 18, 14, 30
    n = len(velas)
    paso_x = (ancho - izq - der) / max(n, 1)
    ancho_vela = max(4.0, paso_x * 0.58)

    def cx(i: int) -> float:
        return izq + (i + 0.5) * paso_x

    def cy(val: float) -> float:
        return sup + (vmax - val) * (alto - sup - inf) / (vmax - vmin)

    s = [f'<svg viewBox="0 0 {ancho} {alto}" class="grafico" role="img">']
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        s.append(f'<line x1="{izq}" x2="{ancho - der}" y1="{cy(v):.1f}" y2="{cy(v):.1f}" class="grid"/>'
                 f'<text x="{izq - 8}" y="{cy(v) + 4:.1f}" class="eje" text-anchor="end">{v:,.0f}</text>')
    for i in range(0, n, max(1, n // 8)):
        s.append(f'<text x="{cx(i):.1f}" y="{alto - 8}" class="eje" text-anchor="middle">'
                 f'{html.escape(velas[i]["hora"])}</text>')
    for i, v in enumerate(velas):
        alcista = v["close"] >= v["open"]
        color = "#34D399" if alcista else "#F87171"
        x_c = cx(i)
        y_high, y_low = cy(v["high"]), cy(v["low"])
        y_top = cy(max(v["open"], v["close"]))
        y_bot = cy(min(v["open"], v["close"]))
        h_body = max(2.0, y_bot - y_top)
        s.append(
            f'<g><line x1="{x_c:.1f}" x2="{x_c:.1f}" y1="{y_high:.1f}" y2="{y_low:.1f}" '
            f'stroke="{color}" stroke-width="1.4"/>'
            f'<rect x="{x_c - ancho_vela / 2:.1f}" y="{y_top:.1f}" width="{ancho_vela:.1f}" '
            f'height="{h_body:.1f}" fill="{color}" rx="1.5"/>'
            f'<title>{v["hora"]} UTC · O:{v["open"]:,.1f} H:{v["high"]:,.1f} '
            f'L:{v["low"]:,.1f} C:{v["close"]:,.1f}</title></g>'
        )
    s.append("</svg>")
    return "".join(s)


def generar_reporte_html(ruta_estado: Path, ruta_html: Path) -> Path:
    """Lee ``estado_bot.json`` y exporta un dashboard HTML autocontenido.

    Incluye KPIs, el estado visual de los 3 cargadores (bala por bala), gráficos
    SVG de PnL y precio, y el historial de acciones simuladas.
    """
    estado = json.loads(ruta_estado.read_text(encoding="utf-8"))
    serie = estado.get("serie", [])
    est = estado["estadisticas"]
    ultimo = serie[-1] if serie else {}
    precio = ultimo.get("precio") or 0.0
    pos = leer_posicion(estado, precio) if precio else None
    total = balas_restantes(estado)
    esc = html.escape

    # ---- KPIs ----
    def kpi(titulo: str, valor: str, sub: str = "", clase: str = "") -> str:
        return (f'<div class="kpi"><span>{esc(titulo)}</span><strong class="{clase}">{valor}</strong>'
                f'<small>{sub}</small></div>')

    pnl_r = est["pnl_realizado_usdt"]
    kpis = [
        kpi("Precio BTC", f"{precio:,.2f}", "USDT · BingX"),
        kpi("PnL no realizado", f"{pos.pnl_pct:+.2f}%" if pos else "—",
            f"{pos.pnl_usdt:+,.2f} USDT" if pos else "sin posición",
            ("pos" if pos.pnl_pct >= 0 else "neg") if pos else ""),
        kpi("Balas restantes", f"{total}/{BALAS_TOTALES}", f"{total / BALAS_TOTALES:.0%} del capital"),
        kpi("Distancia a liquidación", f"{pos.distancia_liquidacion_pct:.2f}%" if pos else "—",
            f"liq. {pos.precio_liquidacion:,.2f}" if pos else "",
            ("neg" if pos.distancia_liquidacion_pct <= UMBRAL_LIQUIDACION_PCT else "") if pos else ""),
        kpi("PnL realizado", f"{pnl_r:+,.2f}", "USDT acumulado", "pos" if pnl_r >= 0 else "neg"),
        kpi("Ciclo actual", f"#{estado['ciclo']['numero']}",
            f"{est['ciclos_cerrados']} cerrados · {est['liquidaciones']} liquidaciones"),
    ]

    # ---- Cargadores ----
    info_c = {"cargador_1": ("Cargador 1", "Operativo diario", "c1"),
              "cargador_2": ("Cargador 2", "Reserva táctica", "c2"),
              "cargador_3": ("Cargador 3", "Reserva extrema · margen", "c3")}
    tarjetas = []
    for clave, (nombre, rol, css) in info_c.items():
        n = estado["cargadores"][clave]
        balas = "".join(f'<i class="bala {css if i < n else "vacia"}"></i>'
                        for i in range(BALAS_POR_CARGADOR))
        tarjetas.append(
            f'<div class="cargador"><div class="cab"><div><h3>{nombre}</h3><small>{rol}</small></div>'
            f'<strong>{n}<span>/{BALAS_POR_CARGADOR}</span></strong></div>'
            f'<div class="balas">{balas}</div>'
            f'<div class="progreso"><div class="{css}" style="width:{n / BALAS_POR_CARGADOR:.0%}">'
            f'</div></div></div>')

    # ---- Gráficos ----
    etiquetas = [p["ejecucion"] for p in serie]
    graf_pnl = svg_lineas(
        etiquetas, [("PnL", [p["pnl_pct"] for p in serie], "#22D3EE", False)],
        [(TAKE_PROFIT_PCT, "TP +15%", "#34D399"), (0, "0%", "#8FB3D9"),
         (-40, "Crítico -40%", "#F26522"), (-60, "Terminal -60%", "#F87171")]
        if any(p["pnl_pct"] is not None and p["pnl_pct"] < -30 for p in serie)
        else [(TAKE_PROFIT_PCT, "TP +15%", "#34D399"), (0, "0%", "#8FB3D9")],
        sufijo="%")
    graf_precio = svg_lineas(etiquetas, [
        ("Precio BTC", [p["precio"] for p in serie], "#22D3EE", False),
        ("Precio de entrada", [p["precio_entrada"] for p in serie], "#F26522", True),
        ("Liquidación", [p["precio_liquidacion"] for p in serie], "#F87171", True),
    ])
    graf_balas = svg_lineas(etiquetas, [("Balas", [p["balas_restantes"] for p in serie],
                                         "#F26522", False)])

    # ---- Telemetría de API & Velas 24h ----
    tele = estado.get("telemetria_api") or {}
    velas_24h = tele.get("velas_1h") or []
    graf_velas_html = svg_velas(velas_24h)
    llamadas_api = tele.get("llamadas") or []
    filas_api = "".join(
        f'<tr><td><code>{esc(c["endpoint"])}</code></td><td><code>{esc(c["metodo"])}</code></td>'
        f'<td><span class="badge ganancia">{esc(c["estado"])}</span></td>'
        f'<td><strong>{c["latencia_ms"]} ms</strong></td></tr>'
        for c in llamadas_api
    ) or '<tr><td colspan="4" class="vacio">Sin llamadas recientes a la API.</td></tr>'
    c24 = tele.get("cambio_24h_pct", 0.0)
    fr_pct = tele.get("funding_rate_pct")
    vol_m = (tele.get("volumen_24h_usdt") or 0) / 1_000_000
    min_24h = tele.get("min_24h", 0)
    max_24h = tele.get("max_24h", 0)
    mark_p = tele.get("mark_price", 0)
    lat_tot = tele.get("latencia_total_ms", 0)
    resumen_api = (
        '<div class="kpis" style="margin-bottom:16px">'
        + kpi("Cambio 24h (BingX)", f"{c24:+.2f}%", f"Rango: {min_24h:,.0f} – {max_24h:,.0f}",
              "pos" if c24 >= 0 else "neg")
        + kpi("Volumen 24h Perpetuo", f"{vol_m:,.1f}M", "USDT operados en BingX")
        + kpi("Funding Rate (8h)", f"{fr_pct:+.4f}%" if fr_pct is not None else "—",
              f"Mark: {mark_p:,.2f} USDT")
        + kpi("Latencia Total API", f"{lat_tot} ms", f"{len(llamadas_api)} endpoints REST HTTP 200")
        + '</div>'
    ) if tele else ""

    # ---- Historial ----
    filas = []
    for ev in reversed(estado["historial"][-80:]):
        etiqueta, categoria = EVENTOS.get(ev["evento"], (ev["evento"], "neutro"))
        pnl = ev.get("pnl_pct")
        balas = (f'{ev.get("balas_compra", 0)} / {ev.get("balas_margen", 0)}'
                 if "balas_compra" in ev else "—")
        filas.append(
            f'<tr><td>{esc(ev["fecha"].replace("T", " ").rstrip("Z"))}</td><td>#{ev["ejecucion"]}</td>'
            f'<td>#{ev["ciclo"]}</td><td><span class="badge {categoria}">{esc(etiqueta)}</span></td>'
            f'<td>{ev["precio"]:,.2f}</td>'
            f'<td class="{"pos" if (pnl or 0) >= 0 else "neg"}">{f"{pnl:+.2f}%" if pnl is not None else "—"}</td>'
            f'<td>{balas}</td><td class="det">{esc(ev["detalle"])}</td></tr>')
    tabla = "".join(filas) or '<tr><td colspan="8" class="vacio">Sin acciones todavía.</td></tr>'

    generado = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    documento = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>3 Cargadores · Reporte de simulación</title>
<style>
:root{{--navy:#0B2A4A;--navy2:#103a63;--orange:#F26522;--cyan:#22D3EE;--cyan2:#67E8F9;--teal:#0891B2;
--text:#E6F1FF;--muted:#8FB3D9;--green:#34D399;--red:#F87171;--card:rgba(255,255,255,.045);--line:rgba(34,211,238,.16)}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI",Inter,system-ui,sans-serif;color:var(--text);min-height:100vh;
background:radial-gradient(1200px 600px at 85% -10%,rgba(34,211,238,.18),transparent 60%),
radial-gradient(900px 500px at -10% 110%,rgba(242,101,34,.14),transparent 60%),var(--navy);padding:32px 20px}}
.wrap{{max-width:1180px;margin:auto;display:grid;gap:22px}}
header{{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:12px}}
h1{{font-size:30px;letter-spacing:.5px}} h1 b{{color:var(--orange)}} h1 em{{color:var(--cyan);font-style:normal}}
header p{{color:var(--muted);margin-top:6px}}
.tag{{border:1px solid var(--orange);color:var(--orange);padding:6px 12px;border-radius:999px;font-size:12px;
font-weight:700;letter-spacing:1.5px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;backdrop-filter:blur(6px)}}
.card h2{{font-size:15px;text-transform:uppercase;letter-spacing:1.6px;color:var(--cyan);margin-bottom:16px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px}}
.kpi{{background:linear-gradient(160deg,rgba(34,211,238,.10),rgba(255,255,255,.02));border:1px solid var(--line);
border-radius:16px;padding:16px;display:grid;gap:6px}}
.kpi span{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:1px}}
.kpi strong{{font-size:24px}} .kpi small{{color:var(--muted);font-size:12px}}
.pos{{color:var(--green)}} .neg{{color:var(--red)}}
.cargadores{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}}
.cargador{{background:rgba(11,42,74,.6);border:1px solid var(--line);border-radius:16px;padding:18px}}
.cab{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}}
.cab h3{{font-size:17px}} .cab small{{color:var(--muted)}}
.cab strong{{font-size:28px}} .cab strong span{{font-size:15px;color:var(--muted)}}
.balas{{display:grid;grid-template-columns:repeat(15,1fr);gap:5px;margin-bottom:14px}}
.bala{{height:22px;border-radius:8px 8px 3px 3px}}
.bala.c1{{background:linear-gradient(var(--cyan2),var(--cyan));box-shadow:0 0 8px rgba(34,211,238,.5)}}
.bala.c2{{background:linear-gradient(var(--cyan),var(--teal))}}
.bala.c3{{background:linear-gradient(#ff8a4c,var(--orange));box-shadow:0 0 8px rgba(242,101,34,.45)}}
.bala.vacia{{background:rgba(255,255,255,.07);border:1px dashed rgba(143,179,217,.25)}}
.progreso{{height:6px;background:rgba(255,255,255,.07);border-radius:9px;overflow:hidden}}
.progreso div{{height:100%;border-radius:9px}} .progreso .c1{{background:var(--cyan)}}
.progreso .c2{{background:var(--teal)}} .progreso .c3{{background:var(--orange)}}
.graficos{{display:grid;grid-template-columns:repeat(auto-fit,minmax(480px,1fr));gap:22px}}
.grafico{{width:100%;height:auto}} .grid{{stroke:rgba(143,179,217,.12)}}
.eje{{fill:var(--muted);font-size:11px}}
.leyenda{{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;color:var(--muted);font-size:13px}}
.leyenda i{{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th{{text-align:left;color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:1px;
padding:10px;border-bottom:1px solid var(--line)}}
td{{padding:10px;border-bottom:1px solid rgba(143,179,217,.08);white-space:nowrap}}
td.det{{white-space:normal;color:var(--muted)}} tr:hover td{{background:rgba(34,211,238,.04)}}
.tabla{{overflow-x:auto;max-height:520px}}
.badge{{padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600}}
.badge.compra{{background:rgba(34,211,238,.14);color:var(--cyan)}}
.badge.defensa{{background:rgba(242,101,34,.16);color:var(--orange)}}
.badge.ganancia{{background:rgba(52,211,153,.15);color:var(--green)}}
.badge.perdida{{background:rgba(248,113,113,.15);color:var(--red)}}
.badge.neutro{{background:rgba(143,179,217,.12);color:var(--muted)}}
.vacio{{color:var(--muted);text-align:center;padding:24px}}
footer{{color:var(--muted);font-size:12.5px;text-align:center;line-height:1.6}} footer b{{color:var(--orange)}}
</style></head><body><div class="wrap">
<header><div><h1><b>3 Cargadores</b> · <em>DCA Bot</em></h1>
<p>BingX · {SYMBOL} · LONG {APALANCAMIENTO}X margen aislado · bala de referencia en .env · generado {generado}</p></div>
<span class="tag">MODO SIMULACIÓN</span></header>
<section class="kpis">{"".join(kpis)}</section>
<section class="card"><h2> Estado de los cargadores · {total}/{BALAS_TOTALES} balas</h2>
<div class="cargadores">{"".join(tarjetas)}</div></section>
<section class="card"><h2>📡 Telemetría de API en Tiempo Real (BingX Perpetual Swap REST v2/v3)</h2>
{resumen_api}
<div class="graficos">
<div><h3 style="font-size:13px;color:var(--muted);margin-bottom:10px;text-transform:uppercase;letter-spacing:1px">Endpoints Consumidos vía CCXT</h3>
<div class="tabla"><table><thead><tr><th>Endpoint HTTP</th><th>Método CCXT</th><th>Estado</th><th>Latencia</th></tr></thead>
<tbody>{filas_api}</tbody></table></div></div>
<div><h3 style="font-size:13px;color:var(--muted);margin-bottom:10px;text-transform:uppercase;letter-spacing:1px">Velas Japonesas Reales (OHLC 1h · Últimas 24h en BingX)</h3>
{graf_velas_html}</div>
</div></section>
<section class="graficos">
<div class="card"><h2>PnL % sobre margen por ejecución</h2>{graf_pnl}
{_leyenda([("PnL %", "#22D3EE"), ("Take Profit", "#34D399")])}</div>
<div class="card"><h2>Precio BTC vs. entrada y liquidación</h2>{graf_precio}
{_leyenda([("Precio BTC", "#22D3EE"), ("Entrada promedio", "#F26522"), ("Liquidación", "#F87171")])}</div>
</section>
<section class="card"><h2>Balas restantes por ejecución</h2>{graf_balas}</section>
<section class="card"><h2>Historial de acciones simuladas</h2><div class="tabla"><table>
<thead><tr><th>Fecha (UTC)</th><th>Ejec.</th><th>Ciclo</th><th>Acción</th><th>Precio</th><th>PnL</th>
<th>Balas C/M</th><th>Detalle</th></tr></thead><tbody>{tabla}</tbody></table></div></section>
<footer><b>Proyecto de código abierto para compartir proyectos y conocimientos técnicos.</b> Todas las operaciones son simuladas; no constituye
asesoramiento financiero.<br>Precio de liquidación aproximado (margen de mantenimiento {TASA_MANTENIMIENTO:.1%}, sin comisiones ni funding).</footer>
</div></body></html>
"""
    ruta_html.write_text(documento, encoding="utf-8")
    return ruta_html


# =========================================================================== #
# CLI
# =========================================================================== #
def parsear_argumentos(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Define los argumentos de línea de comandos."""
    parser = argparse.ArgumentParser(
        description="Bot DCA '3 Cargadores' (SIMULADO) para BTC/USDT:USDT en BingX.")
    parser.add_argument("--precio", type=float, help="usar un precio manual en vez de consultar BingX")
    parser.add_argument("--reset", action="store_true", help="reiniciar la simulación desde cero")
    parser.add_argument("--sin-html", action="store_true", help="no generar reporte.html")
    parser.add_argument("--estado", type=Path, default=RUTA_ESTADO, help="ruta del archivo de estado")
    parser.add_argument("--reporte", type=Path, default=RUTA_REPORTE, help="ruta del reporte HTML")
    return parser.parse_args(argv)


def configurar_consola() -> None:
    """UTF-8 en la salida estándar y soporte ANSI en terminales de Windows."""
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8")
    if os.name == "nt":
        os.system("")  # habilita secuencias VT en la consola clásica de Windows
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada: obtiene precio, simula un día, guarda estado y genera reportes.

    Returns:
        Código de salida (0 = OK, 1 = error).
    """
    configurar_consola()
    args = parsear_argumentos(argv)
    assert DRY_RUN, "Este proyecto solo admite modo simulación."
    cfg = cargar_config()

    estado = estado_inicial() if args.reset else cargar_estado(args.estado)
    if args.reset:
        log.info("Simulación reiniciada.")

    saldo: float | None = None
    if args.precio is not None:
        if args.precio <= 0:
            log.error("--precio debe ser positivo.")
            return 1
        precio, fuente = args.precio, "manual (--precio)"
    else:
        try:
            log.info("Consultando endpoints públicos de %s en BingX (Ticker, Funding, OHLCV)...", SYMBOL)
            exchange = crear_exchange(cfg)
            telemetria = consultar_telemetria_bingx(exchange)
            estado["telemetria_api"] = telemetria
            precio, fuente = telemetria["precio"], "BingX API pública"
            saldo = obtener_saldo(exchange, cfg)
        except ccxt.NetworkError as exc:
            log.error("Sin conexión con BingX tras varios intentos: %s", exc)
            log.error("Tip: usa --precio <valor> para simular sin conexión.")
            return 1
        except ccxt.BaseError as exc:
            log.error("Error de la API de BingX: %s", exc)
            return 1

    eventos = ejecutar_dia(estado, precio, cfg)
    guardar_estado(estado, args.estado)
    imprimir_reporte(estado, precio, eventos, fuente, saldo)

    if not args.sin_html:
        ruta = generar_reporte_html(args.estado, args.reporte)
        log.info("Reporte HTML generado: %s", ruta)
    log.info("Estado guardado en %s", args.estado)
    return 0


if __name__ == "__main__":
    sys.exit(main())
