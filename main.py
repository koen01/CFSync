from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, List
from urllib.request import Request as UrlRequest, urlopen
from urllib.parse import urlparse

import websockets

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from models.schemas import (
    ApiResponse,
    AppState,
    CfsEnvSample,
    FeedRequest,
    JobReallocateSpoolRequest,
    MultiAppState,
    RetractRequest,
    SelectSlotRequest,
    SetAutoRequest,
    SlotState,
    SlotStats,
    SetSpoolmanModeRequest,
    SetSpoolmanUrlRequest,
    SpoolmanLinkRequest,
    SpoolmanUnlinkRequest,
    UiSetColorRequest,
    UiSpoolSetStartRequest,
    UiSlotUpdateRequest,
    UpdateSlotRequest,
)


# ---- Pydantic v1/v2 compatibility helpers ----

def _model_dump(obj) -> dict:
    """Return a plain dict for both Pydantic v1 and v2 models."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj.dict()


def _model_validate(cls, data):
    """Validate/parse a dict into a Pydantic model (v1/v2 compatible)."""
    if hasattr(cls, "model_validate"):
        return cls.model_validate(data)
    return cls.parse_obj(data)


def _req_dump(obj, *, exclude_unset: bool = False) -> dict:
    """Dump request models (v1/v2 compatible) with optional exclude_unset."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_unset=exclude_unset)
    return obj.dict(exclude_unset=exclude_unset)


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
STATIC_DIR = APP_DIR / "static"
STATE_PATH = DATA_DIR / "state.json"
PROFILES_PATH = DATA_DIR / "profiles.json"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_SLOTS = [
    "1A", "1B", "1C", "1D",
    "2A", "2B", "2C", "2D",
    "3A", "3B", "3C", "3D",
    "4A", "4B", "4C", "4D",
]
PRINTER_SPOOL_SLOT = "SP"

# CFS box environment sampling: keep a rolling 24h history (1 sample/min baseline)
_CFS_ENV_MIN_SAMPLE_INTERVAL = 60.0
_CFS_ENV_MAX_POINTS = 24 * 60
_CFS_ENV_TEMP_DELTA = 0.2
_CFS_ENV_HUMIDITY_DELTA = 1.0


def _now() -> float:
    return time.time()


def _parse_iso_ts(val: str) -> Optional[float]:
    try:
        # Accept "Z" and timezone offsets
        if val.endswith("Z"):
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _as_finite_float_or_none(value) -> Optional[float]:
    try:
        vv = float(value)
    except Exception:
        return None
    return vv if math.isfinite(vv) else None


def _coerce_cfs_env_sample(sample) -> Optional[dict]:
    if isinstance(sample, dict):
        ts = _as_finite_float_or_none(sample.get("ts"))
        t = _as_finite_float_or_none(sample.get("temperature_c"))
        h = _as_finite_float_or_none(sample.get("humidity_pct"))
    else:
        ts = _as_finite_float_or_none(getattr(sample, "ts", None))
        t = _as_finite_float_or_none(getattr(sample, "temperature_c", None))
        h = _as_finite_float_or_none(getattr(sample, "humidity_pct", None))
    if not ts or ts <= 0:
        return None
    if t is None and h is None:
        return None
    out = {"ts": ts}
    if t is not None:
        out["temperature_c"] = t
    if h is not None:
        out["humidity_pct"] = h
    return out


def _ensure_data_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    if not PROFILES_PATH.exists():
        PROFILES_PATH.write_text(
            json.dumps(
                {
                    "PLA": {"density_g_cm3": 1.24, "notes": "Default profile"},
                    "ABS": {"density_g_cm3": 1.04, "notes": "Default profile"},
                    "PETG": {"density_g_cm3": 1.27, "notes": "Default profile"},
                    "TPU": {"density_g_cm3": 1.20, "notes": "Default profile"},
                    "ASA": {"density_g_cm3": 1.07, "notes": "Default profile"},
                    "PA": {"density_g_cm3": 1.15, "notes": "Default profile"},
                    "PC": {"density_g_cm3": 1.20, "notes": "Default profile"},
                    "OTHER": {"density_g_cm3": 1.20, "notes": "Fallback"},
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(
                {
                    # Hostname or IPs of printers (used for WebSocket connection at ws://host:9999)
                    # Example: ["192.168.178.148", "192.168.178.149"]
                    "printer_urls": [],
                    # Filament diameter used for mm->g conversion
                    "filament_diameter_mm": 1.75,
                    # Optional: Spoolman URL for spool inventory integration
                    # Example: "http://192.168.178.148:7912"
                    "spoolman_url": "",
                    # Spoolman sync mode: "direct" (CFSync PUTs usage) or
                    # "moonraker" (CFSync calls SET_ACTIVE_SPOOL macro, plugin tracks usage)
                    "spoolman_mode": "direct",
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    if not STATE_PATH.exists():
        state = {
            "printers": {},
            "updated_at": _now(),
        }
        STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def load_profiles() -> dict:
    _ensure_data_files()
    try:
        return json.loads(PROFILES_PATH.read_text())
    except Exception:
        return {}


def _normalize_printer_host(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        host = urlparse(raw).hostname or ""
        return host.strip() if host else ""
    # strip path/port if user pasted host:port or host/path
    host = raw.split("/")[0].strip()
    if ":" in host:
        host = host.split(":", 1)[0].strip()
    return host


def _normalize_printer_id(raw_id: str, address: str) -> str:
    rid = (raw_id or "").strip()
    if rid:
        return rid
    return (address or "").strip()


def _dedupe_printers(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for it in items:
        if not it or it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


def load_config() -> dict:
    _ensure_data_files()
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception:
        cfg = {}

    printer_urls: List[str] = []
    printers: List[dict] = []

    # Backward compat: migrate legacy printer_url into printer_urls
    legacy_printer = _normalize_printer_host(cfg.get("printer_url") or "")
    if legacy_printer:
        printer_urls.append(legacy_printer)

    # Backward compat: extract hostname from legacy moonraker_url if no printer provided
    if not printer_urls:
        mu = (cfg.get("moonraker_url") or "").strip()
        if mu:
            host = urlparse(mu).hostname or ""
            if host:
                print(f"[CONFIG] Migrating moonraker_url → printer_urls (host={host!r})")
                printer_urls.append(host)

    # Preferred multi-printer config
    raw_list = cfg.get("printer_urls")
    if isinstance(raw_list, list):
        for raw in raw_list:
            host = _normalize_printer_host(str(raw))
            if host:
                printer_urls.append(host)

    # Backward/alternate compat: "printers": [{"address": "..."}]
    raw_printers = cfg.get("printers")
    if isinstance(raw_printers, list):
        for item in raw_printers:
            host = ""
            if isinstance(item, dict):
                host = (
                    item.get("address")
                    or item.get("host")
                    or item.get("ip")
                    or item.get("url")
                    or item.get("printer_url")
                    or ""
                )
            elif isinstance(item, str):
                host = item
            host = _normalize_printer_host(str(host))
            if host:
                printer_urls.append(host)

    # Backward/alternate compat: "printers": [{"id": "...", "address": "..."}]
    raw_printers = cfg.get("printers")
    if isinstance(raw_printers, list):
        for item in raw_printers:
            host = ""
            pid = ""
            if isinstance(item, dict):
                host = (
                    item.get("address")
                    or item.get("host")
                    or item.get("ip")
                    or item.get("url")
                    or item.get("printer_url")
                    or ""
                )
                pid = (
                    item.get("id")
                    or item.get("name")
                    or item.get("label")
                    or ""
                )
            elif isinstance(item, str):
                host = item
            host = _normalize_printer_host(str(host))
            if not host:
                continue
            pid = _normalize_printer_id(str(pid), host)
            printers.append({"id": pid, "address": host})
            printer_urls.append(host)

    # Also promote plain printer_urls into printers (id defaults to address)
    existing_addrs = {str(p.get("address") or "").strip() for p in printers}
    for host in printer_urls:
        if host in existing_addrs:
            continue
        pid = _normalize_printer_id("", host)
        printers.append({"id": pid, "address": host})

    # Dedupe by id (keep first)
    seen_ids = set()
    printers_out: List[dict] = []
    for p in printers:
        pid = str(p.get("id") or "").strip()
        addr = str(p.get("address") or "").strip()
        if not pid or not addr or pid in seen_ids:
            continue
        seen_ids.add(pid)
        printers_out.append({"id": pid, "address": addr})

    cfg["printers"] = printers_out
    cfg["printer_urls"] = _dedupe_printers(printer_urls)
    cfg.setdefault("filament_diameter_mm", 1.75)
    cfg.setdefault("spoolman_url", "")
    cfg.setdefault("spoolman_mode", "direct")
    return cfg


def _migrate_app_state_dict(data: dict) -> dict:
    """Make a single-printer AppState tolerant to older/hand-edited formats."""
    if not isinstance(data, dict):
        return data

    # updated_at: allow ISO string
    if isinstance(data.get("updated_at"), str):
        ts = _parse_iso_ts(data["updated_at"])
        if ts is not None:
            data["updated_at"] = ts

    # Some users wrote last_update instead of updated_at
    if "updated_at" not in data and "last_update" in data:
        if data["last_update"] is None:
            data["updated_at"] = 0.0
        elif isinstance(data["last_update"], str):
            data["updated_at"] = _parse_iso_ts(data["last_update"]) or 0.0
        else:
            try:
                data["updated_at"] = float(data["last_update"])
            except Exception:
                data["updated_at"] = 0.0

    # Slots: allow keys like "2A": {material,color,...} without slot field
    slots = data.get("slots", {}) or {}
    if isinstance(slots, dict):
        for slot_id, sd in list(slots.items()):
            if not isinstance(sd, dict):
                continue
            sd.setdefault("slot", slot_id)
            # allow 'color' key
            if "color" in sd and "color_hex" not in sd:
                sd["color_hex"] = sd.pop("color")
            # legacy key 'vendor' -> 'manufacturer'
            if "vendor" in sd and "manufacturer" not in sd:
                sd["manufacturer"] = sd.pop("vendor")
            # tolerate placeholders for material
            mat = sd.get("material")
            if isinstance(mat, str) and mat.strip() in ("", "-", "—", "–"):
                sd["material"] = "OTHER"
            # Spoolman integration (optional)
            sd.setdefault("spoolman_id", None)
            slots[slot_id] = sd
        # ensure all CFS banks exist (1A-4D)
        for sid in (
            "1A", "1B", "1C", "1D",
            "2A", "2B", "2C", "2D",
            "3A", "3B", "3C", "3D",
            "4A", "4B", "4C", "4D",
        ):
            if sid not in slots:
                slots[sid] = {
                    "slot": sid,
                    "material": "OTHER",
                    "color_hex": "#00aaff",
                    "name": "",
                    "manufacturer": "",
                }
        # Ensure the printer's direct spool input exists too.
        if PRINTER_SPOOL_SLOT not in slots:
            slots[PRINTER_SPOOL_SLOT] = {
                "slot": PRINTER_SPOOL_SLOT,
                "material": "OTHER",
                "color_hex": "#00aaff",
                "name": "",
                "manufacturer": "",
                "spoolman_id": None,
            }
        data["slots"] = slots

    data.setdefault("printer_connected", False)
    data.setdefault("printer_last_error", "")

    data.setdefault("cfs_connected", False)
    data.setdefault("cfs_last_update", 0.0)
    data.setdefault("cfs_active_slot", None)
    data.setdefault("cfs_slots", {})
    data.setdefault("ws_slot_length_m", {})
    data.setdefault("cfs_stats", {})
    env_hist_in = data.get("cfs_env_history")
    env_hist_out: Dict[str, list] = {}
    if isinstance(env_hist_in, dict):
        for raw_box_id, raw_samples in env_hist_in.items():
            box_id = str(raw_box_id)
            if box_id not in {"1", "2", "3", "4"}:
                continue
            if not isinstance(raw_samples, list):
                continue
            samples_out = []
            for item in raw_samples:
                coerced = _coerce_cfs_env_sample(item)
                if not coerced:
                    continue
                samples_out.append(coerced)
            if len(samples_out) > _CFS_ENV_MAX_POINTS:
                samples_out = samples_out[-_CFS_ENV_MAX_POINTS:]
            if samples_out:
                env_hist_out[box_id] = samples_out
    data["cfs_env_history"] = env_hist_out
    data.setdefault("job_history", [])

    # Clear the stale "2A" schema default — active_slot is now driven by WS only
    if data.get("active_slot") == "2A":
        data["active_slot"] = None

    return data


# Keep _migrate_state_dict as an alias for backward compat (used in older code paths)
_migrate_state_dict = _migrate_app_state_dict


def _default_printer_id() -> str:
    cfg = load_config()
    printers = cfg.get("printers") or []
    if printers:
        return str((printers[0] or {}).get("id") or "")
    return "printer-1"


def _migrate_multi_state_dict(data: dict) -> dict:
    """Normalize the multi-printer state envelope."""
    if not isinstance(data, dict):
        return {"printers": {}, "updated_at": _now()}

    # Already multi-printer
    if isinstance(data.get("printers"), dict):
        printers_out: Dict[str, dict] = {}
        for pid, raw in (data.get("printers") or {}).items():
            if not isinstance(raw, dict):
                continue
            printers_out[str(pid)] = _migrate_app_state_dict(raw)
        updated_at = data.get("updated_at", _now())
        if isinstance(updated_at, str):
            updated_at = _parse_iso_ts(updated_at) or _now()
        return {
            "printers": printers_out,
            "updated_at": updated_at,
        }

    # Legacy single-printer state
    if isinstance(data.get("slots"), dict):
        pid = _default_printer_id()
        updated_at = data.get("updated_at", _now())
        if isinstance(updated_at, str):
            updated_at = _parse_iso_ts(updated_at) or _now()
        return {
            "printers": {pid: _migrate_app_state_dict(data)},
            "updated_at": updated_at,
        }

    return {"printers": {}, "updated_at": _now()}


_state_load_failed: bool = False  # True when last load fell back to default


def load_state_all() -> MultiAppState:
    global _state_load_failed
    _ensure_data_files()
    try:
        data = json.loads(STATE_PATH.read_text())
        data = _migrate_multi_state_dict(data)
        result = _model_validate(MultiAppState, data)
        # Ensure configured printers exist in state
        cfg_printers = load_config().get("printers") or []
        for p in cfg_printers:
            pid = str((p or {}).get("id") or "")
            if not pid:
                continue
            # If state is keyed by address but config now uses a custom id, migrate it.
            addr = str((p or {}).get("address") or "")
            if pid not in result.printers and addr in result.printers and addr != pid:
                result.printers[pid] = result.printers.pop(addr)
            if pid not in result.printers:
                result.printers[pid] = default_state()
        _state_load_failed = False
        return result
    except Exception as e:
        print(f"[STATE] load failed: {e}")
        _state_load_failed = True
        return default_multi_state()


def save_state_all(state: MultiAppState) -> None:
    if _state_load_failed:
        print("[STATE] save skipped: last load returned fallback default")
        return
    state.updated_at = _now()
    data = json.dumps(_model_dump(state), indent=2, ensure_ascii=False)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(data)
    tmp.rename(STATE_PATH)  # atomic on Linux (same filesystem)


def _all_printer_ids() -> List[str]:
    cfg_printers = load_config().get("printers") or []
    cfg_ids = [str((p or {}).get("id") or "") for p in cfg_printers if (p or {}).get("id")]
    st = load_state_all()
    state_ids = [str(x) for x in st.printers.keys()]
    merged: List[str] = []
    for pid in cfg_ids + state_ids:
        if pid and pid not in merged:
            merged.append(pid)
    return merged


def _resolve_printer_id(printer_id: Optional[str], *, allow_unknown: bool = False) -> str:
    raw = (printer_id or "").strip()
    cfg_ids = {str((p or {}).get("id") or "").strip() for p in (load_config().get("printers") or [])}
    if raw and raw in cfg_ids:
        pid = raw
    else:
        pid = _normalize_printer_host(raw)
    if not pid:
        pid = _default_printer_id()
    else:
        # If caller passed an address, map it to configured id if present
        for p in (load_config().get("printers") or []):
            addr = str((p or {}).get("address") or "").strip()
            cid = str((p or {}).get("id") or "").strip()
            if addr and cid and pid == addr:
                pid = cid
                break
    if not allow_unknown:
        known = set(_all_printer_ids())
        if pid not in known:
            raise HTTPException(status_code=404, detail="Unknown printer")
    return pid


def _printer_address(printer_id: str) -> str:
    pid = (printer_id or "").strip()
    if not pid:
        return ""
    for p in (load_config().get("printers") or []):
        cid = str((p or {}).get("id") or "").strip()
        addr = str((p or {}).get("address") or "").strip()
        if cid and addr and cid == pid:
            return addr
    # Fallback: treat printer_id as host/IP
    return _normalize_printer_host(pid)


def load_state(printer_id: Optional[str] = None) -> AppState:
    pid = _resolve_printer_id(printer_id, allow_unknown=True)
    st = load_state_all()
    state = st.printers.get(pid)
    if state is None:
        return default_state()
    return state


def save_state(printer_id: str, state: AppState) -> None:
    pid = _resolve_printer_id(printer_id, allow_unknown=True)
    st = load_state_all()
    st.printers[pid] = state
    save_state_all(st)


# --- Printer adapter (Dummy) ---
# Keep it minimal: this project is about material management.
# You can later replace these functions with real Moonraker/CFS actions.

def adapter_feed(mm: float) -> None:
    print(f"[ADAPTER] feed {mm}mm")


def adapter_retract(mm: float) -> None:
    print(f"[ADAPTER] retract {mm}mm")


# --- Conversion helpers ---

def mm_to_g(material: str, mm: float) -> float:
    cfg = load_config()
    d_mm = float(cfg.get("filament_diameter_mm", 1.75) or 1.75)
    profiles = load_profiles()
    density = float((profiles.get(material) or {}).get("density_g_cm3", 1.20))

    # grams = density(g/cm^3) * volume(cm^3)
    # volume = area * length
    # area(mm^2) = pi*(d/2)^2 ; to cm^2 => /100
    # length(mm) to cm => /10
    area_cm2 = math.pi * (d_mm / 2.0) ** 2 / 100.0
    length_cm = mm / 10.0
    g = density * area_cm2 * length_cm
    return float(max(0.0, g))


# --- Minimal Moonraker polling (optional) ---

def _http_get_json(url: str, timeout: float = 2.5) -> dict:
    # NOTE: FastAPI also exports a Request type; avoid name clash by using
    # UrlRequest for outbound HTTP requests.
    req = UrlRequest(url, headers={"User-Agent": "filament-manager/1.0"})
    with urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _http_put_json(url: str, body: dict, timeout: float = 3.0) -> dict:
    """PUT JSON body and return parsed response (stdlib only)."""
    data = json.dumps(body).encode("utf-8")
    req = UrlRequest(url, data=data, headers={
        "User-Agent": "filament-manager/1.0",
        "Content-Type": "application/json",
    }, method="PUT")
    with urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


# --- Spoolman integration (optional) ---

def _spoolman_base_url() -> str:
    """Return the configured Spoolman base URL, or empty string if not set."""
    cfg = load_config()
    return (cfg.get("spoolman_url") or "").rstrip("/")


def _spoolman_mode() -> str:
    """Return the Spoolman sync mode: 'direct' or 'moonraker'."""
    return load_config().get("spoolman_mode", "direct")


def _spoolman_get_spools(base: str) -> list[dict]:
    """GET /api/v1/spool — return non-archived spools."""
    url = base + "/api/v1/spool"
    spools = _http_get_json(url, timeout=5.0)
    if not isinstance(spools, list):
        return []
    return [s for s in spools if not s.get("archived", False)]


def _spoolman_get_spool(base: str, spool_id: int) -> dict:
    """GET /api/v1/spool/{id} — return single spool."""
    url = f"{base}/api/v1/spool/{spool_id}"
    return _http_get_json(url, timeout=5.0)


def _spoolman_report_usage(spool_id: int, grams: float) -> None:
    """PUT /api/v1/spool/{id}/use — fire-and-forget."""
    if not spool_id or grams <= 0:
        return
    base = _spoolman_base_url()
    if not base:
        return
    try:
        url = f"{base}/api/v1/spool/{spool_id}/use"
        _http_put_json(url, {"use_weight": round(grams, 2)})
        print(f"[SPOOLMAN] reported usage: spool {spool_id} -= {grams:.2f}g")
    except Exception as e:
        print(f"[SPOOLMAN] usage report failed for spool {spool_id}: {e}")


def _spoolman_report_measure(spool_id: int, weight_g: float) -> None:
    """PUT /api/v1/spool/{id} — set remaining_weight directly. Fire-and-forget."""
    if not spool_id:
        return
    base = _spoolman_base_url()
    if not base:
        return
    try:
        url = f"{base}/api/v1/spool/{spool_id}"
        data = json.dumps({"remaining_weight": round(weight_g, 2)}).encode("utf-8")
        req = UrlRequest(url, data=data, headers={
            "User-Agent": "filament-manager/1.0",
            "Content-Type": "application/json",
        }, method="PATCH")
        with urlopen(req, timeout=3.0) as r:
            r.read()
        print(f"[SPOOLMAN] reported measure: spool {spool_id} = {weight_g:.2f}g")
    except Exception as e:
        print(f"[SPOOLMAN] measure report failed for spool {spool_id}: {e}")


def _spoolman_remaining_weight(spool: dict) -> float:
    try:
        return max(0.0, float(spool.get("remaining_weight") or 0.0))
    except Exception:
        return 0.0


def _spoolman_set_remaining_weight(base: str, spool_id: int, weight_g: float) -> None:
    """PATCH /api/v1/spool/{id} with an exact remaining_weight. Raises on failure."""
    if not spool_id:
        raise ValueError("Invalid spool ID")
    url = f"{base}/api/v1/spool/{spool_id}"
    data = json.dumps({"remaining_weight": round(max(0.0, weight_g), 2)}).encode("utf-8")
    req = UrlRequest(url, data=data, headers={
        "User-Agent": "filament-manager/1.0",
        "Content-Type": "application/json",
    }, method="PATCH")
    with urlopen(req, timeout=5.0) as r:
        r.read()


def _spoolman_id_or_none(value) -> Optional[int]:
    try:
        sid = int(value)
        return sid if sid > 0 else None
    except Exception:
        return None


def _normalize_color_hex(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("0x"):
        raw = raw[2:]
    if raw.startswith("#"):
        raw = raw[1:]
    raw = "".join(ch for ch in raw if ch in "0123456789abcdef")
    if not raw:
        return ""
    # Handle common printer formats:
    # - 0RRGGBB  -> strip leading 0
    # - AARRGGBB -> strip alpha
    # - anything longer -> keep least significant RGB bytes
    if len(raw) == 7 and raw[0] == "0":
        raw = raw[1:]
    elif len(raw) == 8:
        raw = raw[2:]
    elif len(raw) > 8:
        raw = raw[-6:]
    if len(raw) != 6:
        return ""
    return "#" + raw


def _spoolman_job_color_lookup(spool_id: int) -> str:
    """Return current filament color for a spool (cached for UI history rendering)."""
    sid = _spoolman_id_or_none(spool_id)
    if not sid:
        return ""
    now = _now()
    cached = _spoolman_job_color_cache.get(sid)
    if cached and (now - cached[0]) <= _SPOOLMAN_JOB_COLOR_CACHE_TTL:
        return cached[1]

    base = _spoolman_base_url()
    if not base:
        return ""
    try:
        spool = _spoolman_get_spool(base, sid)
        filament = spool.get("filament") or {}
        color = _normalize_color_hex(str(filament.get("color_hex") or ""))
        _spoolman_job_color_cache[sid] = (now, color)
        return color
    except Exception:
        return ""


def _ui_hydrate_job_history_colors(history_in: list) -> list:
    """Hydrate history spool colors from current linked Spoolman spool metadata."""
    if not isinstance(history_in, list):
        return []
    out: list = []
    for job in history_in:
        if not isinstance(job, dict):
            continue
        job_out = dict(job)
        spools_in = job.get("spools") or []
        spools_out: list = []
        if isinstance(spools_in, list):
            for sp in spools_in:
                if not isinstance(sp, dict):
                    continue
                sp_out = dict(sp)
                sid = _spoolman_id_or_none(sp_out.get("spoolman_id"))
                color = _spoolman_job_color_lookup(sid or 0) if sid else ""
                if color:
                    sp_out["color_hex"] = color
                else:
                    sp_out["color_hex"] = _normalize_color_hex(str(sp_out.get("color_hex") or sp_out.get("color") or ""))
                spools_out.append(sp_out)
        job_out["spools"] = spools_out
        out.append(job_out)
    return out


# --- SSH serial number auto-link ---
# Uses sshpass + system ssh binary for compatibility with Creality K2 Plus firmware.
# Tries multiple passwords and multiple file paths to support stock and K2-Improvements firmwares.

_SSH_PASSWORDS = ["creality_2023", "creality_2024", "creality"]
# Per-printer cached working password (reset never — stays valid across reconnects)
_ssh_working_password: Dict[str, Optional[str]] = {}
# Stock firmware path; K2-Improvements moves UDISK to /mnt/UDISK
_SSH_FILE_PATHS = [
    "/mnt/UDISK/creality/userdata/box/material_box_info.json",
    "/usr/data/creality/userdata/box/material_box_info.json",
]

# K2 Pro stores the Spoolman ID encoded in tn_data.json -> base_data -> Tn -> vender.
_TN_DATA_PATHS = [
    "/mnt/UDISK/creality/userdata/box/tn_data.json",
    "/usr/data/creality/userdata/box/tn_data.json",
]


async def _fetch_printer_material_json(printer_id: str) -> Optional[dict]:
    """Fetch material_box_info.json from the printer via sshpass + system ssh."""
    host = (_printer_address(printer_id) or "").strip().split(":")[0]
    if not host:
        return None

    def _ssh_cat() -> Optional[dict]:
        import subprocess
        working = _ssh_working_password.get(printer_id)
        candidates = (
            [working] + [p for p in _SSH_PASSWORDS if p != working]
            if working else _SSH_PASSWORDS
        )
        try:
            for password in candidates:
                # Try all known file paths with this password in one shell command
                cmd = " || ".join(f"cat {p}" for p in _SSH_FILE_PATHS)
                result = subprocess.run(
                    [
                        "sshpass", "-p", password,
                        "ssh",
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=5",
                        f"root@{host}",
                        cmd,
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    if _ssh_working_password.get(printer_id) != password:
                        print(f"[SSH] ({printer_id}) authenticated with password {password!r}")
                        _ssh_working_password[printer_id] = password
                    return json.loads(result.stdout)
                # Exit code 5 = sshpass auth failure — try next password
                if result.returncode != 5:
                    print(f"[SSH] fetch failed ({host}): {result.stderr.strip() or 'no output'}")
                    return None
            print(f"[SSH] all passwords failed for {host}")
            return None
        except FileNotFoundError:
            print("[SSH] sshpass not found; run: apt install sshpass")
            return None
        except Exception as e:
            print(f"[SSH] fetch failed ({host}): {e}")
            return None

    return await asyncio.get_event_loop().run_in_executor(None, _ssh_cat)



def _tn_vendor_spoolman_id(raw: str) -> Optional[int]:
    """
    Extract the Spoolman spool ID encoded by the K2 Pro in tn_data.json.

    The field is a six-character decimal value at positions 28:34:
        ...000010... -> Spoolman #10
        ...000011... -> Spoolman #11
        ...000009... -> Spoolman #9
        ...000002... -> Spoolman #2
    """
    raw = str(raw or "").strip()
    if not raw or raw == "-1" or len(raw) < 34:
        return None

    value = raw[28:34]
    if len(value) != 6 or not value.isdigit():
        return None

    try:
        spool_id = int(value, 10)
    except ValueError:
        return None

    return spool_id if spool_id > 0 else None


async def _fetch_printer_tn_data(printer_id: str) -> Optional[dict]:
    """Fetch K2 Pro tn_data.json from the printer via sshpass/system ssh."""
    host = (_printer_address(printer_id) or "").strip().split(":")[0]
    if not host:
        return None

    def _ssh_cat() -> Optional[dict]:
        import subprocess

        working = _ssh_working_password.get(printer_id)
        candidates = (
            [working] + [p for p in _SSH_PASSWORDS if p != working]
            if working else _SSH_PASSWORDS
        )

        try:
            for password in candidates:
                cmd = " || ".join(f"cat {p}" for p in _TN_DATA_PATHS)
                result = subprocess.run(
                    [
                        "sshpass", "-p", password,
                        "ssh",
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=5",
                        f"root@{host}",
                        cmd,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if result.returncode == 0 and result.stdout.strip():
                    if _ssh_working_password.get(printer_id) != password:
                        print(f"[TN] ({printer_id}) authenticated with password {password!r}")
                        _ssh_working_password[printer_id] = password
                    return json.loads(result.stdout)

                if result.returncode != 5:
                    print(
                        f"[TN] fetch failed ({host}): "
                        f"{result.stderr.strip() or 'no output'}"
                    )
                    return None

            print(f"[TN] all passwords failed for {host}")
            return None

        except FileNotFoundError:
            print("[TN] sshpass not found; run: apt install sshpass")
            return None
        except Exception as e:
            print(f"[TN] fetch failed ({host}): {e}")
            return None

    return await asyncio.get_event_loop().run_in_executor(None, _ssh_cat)


def _apply_tn_data_links(info: dict, printer_id: str) -> None:
    """
    Auto-link CFS slots to Spoolman using K2 Pro tn_data.json.

    Mapping:
        T1 vender[0..3] -> 1A..1D
        T2 vender[0..3] -> 2A..2D
        T3 vender[0..3] -> 3A..3D
        T4 vender[0..3] -> 4A..4D
    """
    if not isinstance(info, dict):
        return

    base_data = info.get("base_data")
    if not isinstance(base_data, dict):
        return

    base = _spoolman_base_url()
    st = load_state(printer_id)
    changed = False

    for box_num in range(1, 5):
        box = base_data.get(f"T{box_num}")
        if not isinstance(box, dict):
            continue

        vendors = box.get("vender")
        if not isinstance(vendors, list):
            continue

        for index, raw in enumerate(vendors[:4]):
            spool_id = _tn_vendor_spoolman_id(raw)
            if not spool_id:
                continue

            slot = f"{box_num}{'ABCD'[index]}"
            if slot not in _VALID_CFS_SLOT_IDS:
                continue

            # Only link IDs that really exist in Spoolman. If Spoolman is
            # temporarily unavailable, keep the current mapping untouched.
            if base:
                try:
                    spool = _spoolman_get_spool(base, spool_id)
                    if not isinstance(spool, dict) or not spool.get("id"):
                        print(
                            f"[TN] ({printer_id}) Slot {slot}: "
                            f"spool #{spool_id} not found in Spoolman"
                        )
                        continue
                except Exception as e:
                    print(
                        f"[TN] ({printer_id}) Slot {slot}: "
                        f"Spoolman lookup failed for spool #{spool_id}: {e}"
                    )
                    continue

            slot_obj = st.slots.get(slot)
            if slot_obj is None:
                slot_obj = SlotState(slot=slot)

            old_id = getattr(slot_obj, "spoolman_id", None)
            if old_id == spool_id:
                continue

            slot_obj.spoolman_id = spool_id
            st.slots[slot] = slot_obj
            changed = True

            print(
                f"[TN] ({printer_id}) Slot {slot}: "
                f"linked Spoolman {old_id!r} -> #{spool_id} "
                f"from tn_data.json"
            )

    if changed:
        save_state(printer_id, st)


def _apply_serialnum_links(info: dict, printer_id: str) -> None:
    """Parse material_box_info.json; link slots whose serialNum is a valid Spoolman spool ID."""
    base = _spoolman_base_url()
    st = load_state(printer_id)
    changed = False

    boxes = (info.get("Material", {}).get("info") or [])
    print(f"[SSH] ({printer_id}) material_box_info.json contains {len(boxes)} box(es): "
          f"{[b.get('boxID') for b in boxes if isinstance(b, dict)]}")

    for box in boxes:
        box_id_str = box.get("boxID", "")   # "T1" .. "T4"
        if not box_id_str.startswith("T"):
            continue
        box_num = box_id_str[1:]

        for mat in (box.get("list") or []):
            mat_id = mat.get("materialId", "")   # "A" .. "D"
            slot = f"{box_num}{mat_id}"
            if slot not in _VALID_CFS_SLOT_IDS:
                continue

            serial = (mat.get("serialNum") or "").strip()
            if not serial or serial == "000000":
                continue
            try:
                spool_id = int(serial)
            except ValueError:
                continue
            if spool_id <= 0:
                continue

            slot_obj = st.slots.get(slot)
            if slot_obj and getattr(slot_obj, "spoolman_id", None) == spool_id:
                continue  # already linked

            if base:
                try:
                    spool = _http_get_json(f"{base}/api/v1/spool/{spool_id}", timeout=5.0)
                    if not isinstance(spool, dict) or not spool.get("id"):
                        print(f"[SSH] Slot {slot}: serialNum {serial!r} → spool {spool_id} not in Spoolman")
                        continue
                except Exception as e:
                    print(f"[SSH] Slot {slot}: Spoolman lookup failed for spool {spool_id}: {e}")
                    continue

            if slot_obj is None:
                slot_obj = SlotState(slot=slot)
            slot_obj.spoolman_id = spool_id
            st.slots[slot] = slot_obj
            changed = True
            print(f"[SSH] Slot {slot}: linked → Spoolman spool {spool_id} via serialNum {serial!r}")

    if changed:
        save_state(printer_id, st)


async def _ssh_fetch_and_apply(printer_id: str) -> None:
    """
    Refresh CFS -> Spoolman links from printer-side data.

    K2 Pro prefers tn_data.json because its 'vender' field contains the
    Spoolman ID encoded in the RFID data. material_box_info.json/serialNum
    remains as a compatibility fallback for other firmware/models.
    """
    try:
        tn_info = await _fetch_printer_tn_data(printer_id)
        if tn_info:
            _apply_tn_data_links(tn_info, printer_id)
    except Exception as e:
        print(f"[TN] ({printer_id}) auto-link failed: {e}")

    try:
        info = await _fetch_printer_material_json(printer_id)
        if info:
            _apply_serialnum_links(info, printer_id)
    except Exception as e:
        print(f"[SSH] ({printer_id}) serialNum auto-link failed: {e}")


def _color_distance(hex1: str, hex2: str) -> float:
    """Simple Euclidean RGB distance between two hex colors."""
    try:
        h1 = hex1.lstrip("#")
        h2 = hex2.lstrip("#")
        r1, g1, b1 = int(h1[0:2], 16), int(h1[2:4], 16), int(h1[4:6], 16)
        r2, g2, b2 = int(h2[0:2], 16), int(h2[2:4], 16), int(h2[4:6], 16)
        return math.sqrt((r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2)
    except Exception:
        return 999.0


_WS_SAVE_INTERVAL = 10.0
_ws_last_save: Dict[str, float] = {}
_ws_last_rfid: Dict[str, Dict[str, str]] = {}   # printer_id → slot → RFID
_ws_last_state: Dict[str, Dict[str, int]] = {}  # printer_id → slot → CFS state (0/1/2)
_ws_last_fingerprint: Dict[str, Dict[str, str]] = {}  # printer_id → slot → material fingerprint

_SSH_FETCH_COOLDOWN = 30.0  # seconds between SSH fetches of material_box_info.json
_ssh_last_fetch: Dict[str, float] = {}

_moon_last_state: Dict[str, str] = {}        # printer_id → last known print_stats.state
_moon_last_filament_mm: Dict[str, float] = {}                   # printer_id → filament_used at last poll tick
_moon_job_track_slot_g: Dict[str, Dict[str, float]] = {}         # printer_id → slot → grams
_moon_job_track_slot_mm: Dict[str, Dict[str, float]] = {}        # printer_id → slot → mm
_moon_job_started_at: Dict[str, float] = {}                       # printer_id → Unix timestamp
_moon_job_name: Dict[str, str] = {}                               # printer_id → filename/job name
_moon_live_status: Dict[str, dict] = {}      # printer_id → live temps/progress (in-memory only)

_VALID_CFS_SLOT_IDS = frozenset(
    f"{b}{l}" for b in "1234" for l in "ABCD"
)

# Log unknown WS message top-level keys once per session to aid discovery
_ws_seen_keys: Dict[str, set] = {}

# Spoolman-derived percent cache for manual (non-RFID) slots
_spoolman_manual_pct: Dict[str, Dict[str, Optional[int]]] = {}  # printer_id → slot → percent or None
_spoolman_pct_refresh_at: Dict[str, Dict[str, float]] = {}      # printer_id → slot → next refresh timestamp
_SPOOLMAN_PCT_TTL = 60.0
_spoolman_job_color_cache: Dict[int, tuple[float, str]] = {}    # spool_id → (ts, "#rrggbb"|"")
_SPOOLMAN_JOB_COLOR_CACHE_TTL = 30.0

# Known WS key names for printer identity (tried in order)
_WS_NAME_KEYS = ("hostname", "machineName", "printerName", "deviceName", "model", "MachineModel", "deviceModel")
_WS_FW_KEYS   = ("softVersion", "firmwareVersion", "version", "FirmwareVersion", "SoftwareVersion", "firmware")


def _printer_ws_url(printer_id: str) -> str:
    host = _printer_address(printer_id)
    if not host:
        return ""
    return f"ws://{host.split(':')[0]}:9999"


def _moonraker_base_url(printer_id: str) -> str:
    """Return the Moonraker HTTP base URL (port 7125), or empty string if not configured."""
    cfg = load_config()
    mu = (cfg.get("moonraker_url") or "").strip()
    if mu:
        parsed = urlparse(mu)
        host = parsed.hostname or ""
        port = parsed.port or 7125
        cfg_ids = [str((p or {}).get("id") or "") for p in (cfg.get("printers") or [])]
        if len(cfg_ids) <= 1 or host == _printer_address(printer_id):
            return f"http://{host}:{port}"
    host = (_printer_address(printer_id) or "").split(":")[0]
    return f"http://{host}:7125" if host else ""


def _moonraker_send_gcode(printer_id: str, script: str) -> bool:
    """POST a gcode script to Moonraker. Returns True on success."""
    base = _moonraker_base_url(printer_id)
    if not base:
        print(f"[MOON] ({printer_id}) send_gcode skipped — no Moonraker URL configured (script: {script!r})")
        return False
    url = f"{base}/printer/gcode/script"
    try:
        body = json.dumps({"script": script}).encode()
        req = UrlRequest(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5.0) as resp:
            if resp.status != 200:
                print(f"[MOON] ({printer_id}) send_gcode HTTP {resp.status} for {script!r}")
                return False
        return True
    except Exception as exc:
        print(f"[MOON] ({printer_id}) send_gcode exception for {script!r}: {exc}")
        return False


def _fetch_webcam_url(printer_id: str) -> str:
    """Query Moonraker's webcam list and return the first enabled stream URL.

    Relative URLs (e.g. /webcam/?action=stream) are resolved against port 4408
    which is the standard nginx proxy port on Creality K1/K1C firmware.
    """
    base = _moonraker_base_url(printer_id)
    if not base:
        return ""
    try:
        data = _http_get_json(f"{base}/server/webcams/list", timeout=5.0)
        webcams = (data.get("result") or {}).get("webcams") or []
        for wc in webcams:
            if not wc.get("enabled", True):
                continue
            stream_url = str(wc.get("stream_url") or "").strip()
            if not stream_url:
                continue
            if stream_url.startswith("http"):
                return stream_url
            parsed = urlparse(base)
            return f"{parsed.scheme}://{parsed.hostname}:4408{stream_url}"
        return ""
    except Exception as e:
        print(f"[MOON] ({printer_id}) webcam URL fetch failed: {e}")
        return ""


def _moonraker_set_active_spool(printer_id: str, spool_id: Optional[int]) -> None:
    """Call SET_ACTIVE_SPOOL or CLEAR_ACTIVE_SPOOL on the printer via Moonraker.

    Runs the blocking HTTP call in a background thread so neither the event
    loop nor a sync worker thread is blocked (the gcode POST can take up to
    5 s to time out).  Using a plain thread avoids asyncio.ensure_future
    failing when called from a sync FastAPI endpoint running in an AnyIO
    worker thread that has no current event loop.
    """
    cmd = f"SET_ACTIVE_SPOOL ID={spool_id}" if spool_id else "CLEAR_ACTIVE_SPOOL"

    def _run() -> None:
        ok = _moonraker_send_gcode(printer_id, cmd)
        print(f"[MOON] ({printer_id}) {cmd} — {'OK' if ok else 'FAILED'}")

    threading.Thread(target=_run, daemon=True).start()


def _normalize_ws_color(raw: str) -> str:
    """Normalize printer color payloads to '#rrggbb'."""
    return _normalize_color_hex(raw)


def _cfs_env_value_changed(prev: Optional[float], cur: Optional[float], min_delta: float) -> bool:
    if cur is None:
        return prev is not None
    if prev is None:
        return True
    return abs(cur - prev) >= min_delta


def _record_cfs_env_sample(
    st: AppState,
    box_id: int,
    *,
    ts: float,
    temperature_c: Optional[float],
    humidity_pct: Optional[float],
) -> None:
    box_key = str(box_id)
    temp = _as_finite_float_or_none(temperature_c)
    hum = _as_finite_float_or_none(humidity_pct)
    if temp is None and hum is None:
        return

    raw_hist = st.cfs_env_history.get(box_key) or []
    hist: list[CfsEnvSample] = []
    if isinstance(raw_hist, list):
        for item in raw_hist:
            coerced = _coerce_cfs_env_sample(item)
            if coerced:
                hist.append(_model_validate(CfsEnvSample, coerced))

    should_append = True
    if hist:
        last = hist[-1]
        last_ts = _as_finite_float_or_none(last.ts) or 0.0
        dt = ts - last_ts
        prev_t = _as_finite_float_or_none(last.temperature_c)
        prev_h = _as_finite_float_or_none(last.humidity_pct)
        temp_changed = _cfs_env_value_changed(prev_t, temp, _CFS_ENV_TEMP_DELTA)
        hum_changed = _cfs_env_value_changed(prev_h, hum, _CFS_ENV_HUMIDITY_DELTA)
        should_append = (dt >= _CFS_ENV_MIN_SAMPLE_INTERVAL) or temp_changed or hum_changed

    if not should_append:
        return

    hist.append(CfsEnvSample(
        ts=float(ts),
        temperature_c=round(temp, 2) if temp is not None else None,
        humidity_pct=round(hum, 2) if hum is not None else None,
    ))
    if len(hist) > _CFS_ENV_MAX_POINTS:
        hist = hist[-_CFS_ENV_MAX_POINTS:]
    st.cfs_env_history[box_key] = hist


def _parse_ws_printer_info(payload: dict, printer_id: str) -> None:
    """Extract printer name / firmware from any WS status message and persist to state.

    Also logs any previously-unseen top-level keys once per session so we can
    discover the exact field names the printer uses.
    """
    seen = _ws_seen_keys.setdefault(printer_id, set())
    new_keys = set(payload.keys()) - seen
    if new_keys:
        seen |= new_keys
        _ws_seen_keys[printer_id] = seen
        print(f"[WS] ({printer_id}) New message keys: {sorted(new_keys)}")

    name = ""
    for k in _WS_NAME_KEYS:
        v = str(payload.get(k) or "").strip()
        if v:
            name = v
            break

    fw = ""
    for k in _WS_FW_KEYS:
        v = str(payload.get(k) or "").strip()
        if v:
            fw = v
            break
    # Parse "modelVersion" field: "printer hw ver:;printer sw ver:;DWIN sw ver:1.1.3.13;"
    if not fw:
        mv = str(payload.get("modelVersion") or "").strip()
        if mv:
            for part in mv.split(";"):
                part = part.strip()
                if "sw ver:" in part.lower() and ":" in part:
                    ver = part.split(":", 1)[1].strip()
                    if ver:
                        fw = ver
                        break

    if not name and not fw:
        return

    st = load_state(printer_id)
    changed = False
    if name and name != st.printer_name:
        st.printer_name = name
        changed = True
        print(f"[WS] ({printer_id}) Printer name: {name!r}")
    if fw and fw != st.printer_firmware:
        st.printer_firmware = fw
        changed = True
        print(f"[WS] ({printer_id}) Firmware: {fw!r}")
    if changed:
        save_state(printer_id, st)


# Per-printer in-process active slot tracker (for Moonraker mode notification)
_ws_active_slot: Dict[str, Any] = {}
_WS_ACTIVE_SLOT_SENTINEL = object()


def _parse_ws_cfs_data(payload: dict, printer_id: str) -> None:
    """Parse a boxsInfo WS payload and update local state + Spoolman."""
    try:
        boxes = (payload.get("boxsInfo") or {}).get("materialBoxs") or []
    except Exception:
        return

    st = load_state(printer_id)
    last_rfid = _ws_last_rfid.setdefault(printer_id, {})
    last_state = _ws_last_state.setdefault(printer_id, {})
    last_fingerprint = _ws_last_fingerprint.setdefault(printer_id, {})
    manual_pct = _spoolman_manual_pct.setdefault(printer_id, {})
    active_slot: Optional[str] = None
    boxes_meta: dict = {}
    seen_slots: set[str] = set()
    now_ts = _now()

    def _process_material_slot(slot: str, mat: dict, *, allow_ssh_serial_lookup: bool) -> None:
        nonlocal active_slot
        raw_state_val = int(mat.get("state") or 0)
        mat_type_raw = str(mat.get("type") or "").strip().upper()
        name_raw = str(mat.get("name") or "").strip()
        vendor_raw = str(mat.get("vendor") or "").strip()
        rfid_raw = str(mat.get("rfid") or "").strip()
        raw_color = mat.get("color", "")
        color_norm = _normalize_ws_color(raw_color)
        slot_fingerprint = "|".join([mat_type_raw, name_raw, vendor_raw, (color_norm or "").lower()])
        rfid_missing = rfid_raw in ("", "0", "00", "000", "0000", "00000", "000000")
        # Creality's "empty spool" option may come through as manual (state=1)
        # with a placeholder material and no identifying metadata. Treat that as
        # truly empty so UI/rendering does not show "OTHER".
        empty_manual_signature = (
            raw_state_val == 1
            and rfid_missing
            and not name_raw
            and not vendor_raw
            and mat_type_raw in ("", "-", "—", "–", "N/A", "NA", "NONE", "OTHER")
        )
        state_val = 0 if empty_manual_signature else raw_state_val
        selected = int(mat.get("selected") or 0)

        # state 2 = RFID: use Spoolman-based calc (same behavior as manual slots)
        # state 1 = manual: WS always reports 100 (no sensor) → use Spoolman cache
        # state 0 = empty: no percent
        if state_val in (1, 2):
            pct = manual_pct.get(slot)  # None until async refresh fills it
        else:
            pct = None

        st.cfs_slots[slot] = {
            "percent": pct,
            "state": state_val,
            "rfid": rfid_raw,
            "selected": selected,
            "present": state_val > 0,
            "material": mat_type_raw if state_val > 0 else "",
            "color": _normalize_color_hex(color_norm) if state_val > 0 else "",
            "name": name_raw if state_val > 0 else "",
            "manufacturer": vendor_raw if state_val > 0 else "",
        }
        seen_slots.add(slot)

        if selected == 1 and state_val > 0:
            active_slot = slot

        # Update local slot metadata from WS data (only if a spool is physically present)
        if state_val > 0 and slot in st.slots:
            slot_obj = st.slots[slot]
            if color_norm and len(color_norm) == 7 and color_norm.startswith("#"):
                slot_obj.color_hex = color_norm
            mat_type = (mat.get("type") or "").strip().upper()
            if mat_type:
                slot_obj.material = mat_type  # type: ignore[assignment]
            name = (mat.get("name") or "").strip()
            if name:
                slot_obj.name = name
            vendor = (mat.get("vendor") or "").strip()
            if vendor:
                slot_obj.manufacturer = vendor
            st.slots[slot] = slot_obj

        def _clear_slot_link(reason: str) -> None:
            slot_obj_swap = st.slots.get(slot)
            if slot_obj_swap and getattr(slot_obj_swap, "spoolman_id", None):
                if _spoolman_mode() == "moonraker" and (
                    active_slot == slot
                    or (slot == PRINTER_SPOOL_SLOT and not active_slot)
                ):
                    _moonraker_set_active_spool(printer_id, None)
                slot_obj_swap.spoolman_id = None
                st.slots[slot] = slot_obj_swap
                st.ws_slot_length_m.pop(slot, None)
                last_rfid.pop(slot, None)
                manual_pct.pop(slot, None)
                _spoolman_pct_refresh_at.setdefault(printer_id, {}).pop(slot, None)
                print(f"[CFS] ({printer_id}) Slot {slot}: {reason}, unlinked Spoolman spool")

        # Detect spool removal/swap and unlink Spoolman.
        prev_state = last_state.get(slot, -1)
        last_state[slot] = state_val
        removed_or_swapped = (prev_state == 2 and state_val != 2) or (prev_state > 0 and state_val == 0)
        if removed_or_swapped:
            _clear_slot_link(f"state {prev_state}→{state_val}")

        # Detect manual filament metadata changes while state stays loaded.
        if state_val > 0:
            prev_fp = last_fingerprint.get(slot, "")
            if prev_fp and slot_fingerprint and prev_fp != slot_fingerprint:
                _clear_slot_link("filament metadata changed")
            if slot_fingerprint:
                last_fingerprint[slot] = slot_fingerprint
        else:
            last_fingerprint.pop(slot, None)

        # K2 Pro / serialNum auto-link is only available for CFS slots.
        # Refresh on first detection/insertion and periodically afterwards.
        # Reserve the cooldown before creating the task so A/B/C/D do not
        # start several simultaneous SSH sessions from the same WS payload.
        if allow_ssh_serial_lookup and state_val == 2:
            now = time.time()
            last_fetch = _ssh_last_fetch.get(printer_id, 0.0)
            if prev_state != 2 or now - last_fetch > _SSH_FETCH_COOLDOWN:
                _ssh_last_fetch[printer_id] = now
                asyncio.create_task(_ssh_fetch_and_apply(printer_id))

        # boxsInfo["rfid"] is not a unique physical spool UID on K2 Pro.
        # Different spools may expose the same value, so keep it only as
        # diagnostic/cache data and NEVER unlink Spoolman based on it.
        rfid = mat.get("rfid", "")
        if rfid and state_val == 2:
            last_rfid[slot] = str(rfid)

        # Track cumulative length for per-job Moonraker attribution
        cur_m = float(mat.get("usedMaterialLength") or 0)
        st.ws_slot_length_m[slot] = cur_m

    for box in boxes:
        if not isinstance(box, dict):
            continue
        box_type = box.get("type")
        if box_type == 0:
            box_id = box.get("id")
            if not isinstance(box_id, int) or box_id < 1 or box_id > 4:
                continue

            box_temp = float(box["temp"]) if isinstance(box.get("temp"), (int, float)) else None
            box_humidity = float(box["humidity"]) if isinstance(box.get("humidity"), (int, float)) else None
            boxes_meta[str(box_id)] = {
                "connected": True,
                "temperature_c": box_temp,
                "humidity_pct": box_humidity,
            }
            _record_cfs_env_sample(
                st,
                box_id,
                ts=now_ts,
                temperature_c=box_temp,
                humidity_pct=box_humidity,
            )

            for mat in (box.get("materials") or []):
                if not isinstance(mat, dict):
                    continue
                mat_id = mat.get("id")
                if not isinstance(mat_id, int) or mat_id < 0 or mat_id > 3:
                    continue

                slot = f"{box_id}{'ABCD'[mat_id]}"
                if slot not in _VALID_CFS_SLOT_IDS:
                    continue
                _process_material_slot(slot, mat, allow_ssh_serial_lookup=True)
            continue

        if box_type == 1:
            # Direct printer spool holder (single input, outside CFS boxes).
            # Firmware sends this as a dedicated holder with one material entry.
            mats = box.get("materials") or []
            first = mats[0] if isinstance(mats, list) and mats and isinstance(mats[0], dict) else {}
            _process_material_slot(PRINTER_SPOOL_SLOT, first, allow_ssh_serial_lookup=False)

    # If the current payload did not include spool-holder data, keep SP visible but mark empty.
    if PRINTER_SPOOL_SLOT not in seen_slots:
        st.cfs_slots[PRINTER_SPOOL_SLOT] = {
            "percent": None,
            "state": 0,
            "rfid": "",
            "selected": 0,
            "present": False,
            "material": "",
            "color": "",
            "name": "",
            "manufacturer": "",
        }

    # Store box connection metadata so the frontend can show correct boxes.
    # If no CFS boxes are present, clear stale metadata.
    if boxes_meta:
        st.cfs_slots["_boxes"] = boxes_meta
    else:
        st.cfs_slots.pop("_boxes", None)

    # Always update active slot — clears stale value when printer is idle
    st.cfs_active_slot = active_slot
    if active_slot and active_slot in st.slots:
        st.active_slot = active_slot
    else:
        st.active_slot = None

    # Moonraker mode: notify printer when the active CFS slot changes.
    # Only fire when the firmware explicitly selects a CFS slot (active_slot is non-None).
    # SP activation is handled at job-start time in moonraker_job_poll_loop to avoid
    # spurious SET_ACTIVE_SPOOL calls during multi-color slot transitions (firmware
    # briefly sends selected=0 for all CFS slots while switching).
    prev_active = _ws_active_slot.get(printer_id, _WS_ACTIVE_SLOT_SENTINEL)
    if _spoolman_mode() == "moonraker" and active_slot and active_slot != prev_active:
        new_spool_id = (st.slots[active_slot].spoolman_id
                        if active_slot in st.slots else None)
        _moonraker_set_active_spool(printer_id, new_spool_id)
    _ws_active_slot[printer_id] = active_slot

    # Direct spool holder (SP) is not a CFS. Only mark connected when at least
    # one CFS box (type 0) is present in the current payload.
    st.cfs_connected = bool(boxes_meta)
    st.cfs_last_update = _now()
    st.printer_connected = True
    st.printer_last_error = ""

    now = _now()
    if now - _ws_last_save.get(printer_id, 0.0) >= _WS_SAVE_INTERVAL:
        save_state(printer_id, st)
        _ws_last_save[printer_id] = now


async def _refresh_manual_slot_pcts(printer_id: str) -> None:
    """Calculate Spoolman-based percent for all linked slots (manual and RFID) and cache it.

    Called after each boxsInfo parse. Uses a per-slot TTL so Spoolman is queried
    at most once per _SPOOLMAN_PCT_TTL seconds per slot.
    """
    base = _spoolman_base_url()
    if not base:
        return
    st = load_state(printer_id)
    now = _now()
    loop = asyncio.get_running_loop()
    manual_pct = _spoolman_manual_pct.setdefault(printer_id, {})
    pct_refresh = _spoolman_pct_refresh_at.setdefault(printer_id, {})

    for slot, cfs_meta in list(st.cfs_slots.items()):
        if not isinstance(cfs_meta, dict) or cfs_meta.get("state") not in (1, 2):
            continue
        slot_obj = st.slots.get(slot)
        spool_id = getattr(slot_obj, "spoolman_id", None) if slot_obj else None
        if not spool_id:
            manual_pct.pop(slot, None)
            continue
        if pct_refresh.get(slot, 0) > now:
            continue  # still fresh

        try:
            sp = await loop.run_in_executor(None, _spoolman_get_spool, base, spool_id)
            filament = sp.get("filament") or {}
            nominal_g = float(filament.get("weight") or 0)
            remaining_g = float(sp.get("remaining_weight") or 0)
            used_g = float(sp.get("used_weight") or 0)
            if nominal_g > 0:
                pct: Optional[int] = max(0, min(100, int(round(remaining_g / nominal_g * 100))))
            elif remaining_g + used_g > 0:
                pct = max(0, min(100, int(round(remaining_g / (remaining_g + used_g) * 100))))
            else:
                pct = None
            manual_pct[slot] = pct
            pct_refresh[slot] = now + _SPOOLMAN_PCT_TTL
            state_label = "RFID" if cfs_meta.get("state") == 2 else "manual"
            print(f"[SPOOLMAN] ({printer_id}) Slot {slot} {state_label} percent: {pct}%")
        except Exception:
            pct_refresh[slot] = now + 10.0  # back off on error


async def _ws_connect_and_run(ws_url: str, printer_id: str) -> None:
    """Open one WebSocket connection to the printer and run the polling loop."""
    async with websockets.connect(ws_url, ping_interval=None, ping_timeout=None) as ws:
        # Consume the very first burst (max 5 messages, 0.15 s each).
        # Parse for printer identity (hostname/modelVersion) but skip CFS data,
        # which may be stale at this point.
        for _ in range(5):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.15)
                try:
                    _parse_ws_printer_info(json.loads(msg), printer_id)
                except Exception:
                    pass
            except asyncio.TimeoutError:
                break

        # Heartbeat handshake. The printer may push status frames before "ok",
        # so scan up to 10 messages instead of assuming the very next one is the ack.
        await ws.send(json.dumps({"ModeCode": "heart_beat"}))
        for _ in range(10):
            try:
                reply = await asyncio.wait_for(ws.recv(), timeout=2.0)
                if str(reply).strip() == "ok":
                    break
            except asyncio.TimeoutError:
                break

        st = load_state(printer_id)
        st.printer_connected = True
        st.printer_last_error = ""
        save_state(printer_id, st)
        print(f"[WS] ({printer_id}) Connected to {ws_url}")

        # Request initial CFS data immediately after handshake
        await ws.send(json.dumps({"method": "get", "params": {"boxsInfo": 1}}))
        _last_request: float = asyncio.get_event_loop().time()

        # Continuous message loop — process everything the printer sends.
        # Never assume the next recv() is the response to our request; the printer
        # pushes status frames continuously between our request and its reply.
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=6.0)
            except asyncio.TimeoutError:
                # Printer went silent — re-request and wait again
                await ws.send(json.dumps({"method": "get", "params": {"boxsInfo": 1}}))
                _last_request = asyncio.get_event_loop().time()
                continue

            # Printer heartbeat ping — ack it immediately
            if isinstance(msg, str) and "heart_beat" in msg:
                await ws.send("ok")
                continue

            # Plain "ok" is the printer acking our heartbeat — nothing to do
            if isinstance(msg, str) and msg.strip() == "ok":
                continue

            try:
                data = json.loads(msg)
                _parse_ws_printer_info(data, printer_id)
                if "boxsInfo" in data:
                    _parse_ws_cfs_data(data, printer_id)
                    asyncio.create_task(_refresh_manual_slot_pcts(printer_id))
            except Exception:
                pass

            # Re-request every 5 s so we keep receiving fresh pushes
            now = asyncio.get_event_loop().time()
            if now - _last_request >= 5.0:
                await ws.send(json.dumps({"method": "get", "params": {"boxsInfo": 1}}))
                _last_request = now


async def printer_ws_loop(printer_id: str) -> None:
    """Outer reconnect loop for the printer WebSocket connection."""
    ws_url = _printer_ws_url(printer_id)
    if not ws_url:
        print(f"[WS] ({printer_id}) No printer ID configured — WebSocket loop not started.")
        return

    print(f"[WS] ({printer_id}) Starting WebSocket loop for {ws_url}")
    backoff = 2.0

    while True:
        last_err = ""
        try:
            await _ws_connect_and_run(ws_url, printer_id)
            backoff = 2.0  # reset on clean exit
        except Exception as e:
            last_err = str(e)
            print(f"[WS] ({printer_id}) Connection lost: {e}")

        try:
            st = load_state(printer_id)
            st.printer_connected = False
            st.cfs_connected = False
            st.printer_last_error = last_err
            save_state(printer_id, st)
        except Exception:
            pass

        print(f"[WS] ({printer_id}) Reconnecting in {backoff:.0f}s…")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


def _moon_flush_to_spoolman(
    printer_id: str,
    reason: str,
    *,
    started_at: Optional[float] = None,
    ended_at: Optional[float] = None,
    job_name: str = "",
) -> None:
    """Sync accumulated per-slot grams to Spoolman, persist job stats, and reset trackers."""
    st = load_state(printer_id)
    job_g = _moon_job_track_slot_g.setdefault(printer_id, {})
    job_mm = _moon_job_track_slot_mm.setdefault(printer_id, {})
    ended_ts = ended_at or _now()
    started_ts = started_at or ended_ts
    spools: List[dict] = []
    total_grams = 0.0
    total_meters = 0.0
    spoolman_base = _spoolman_base_url()
    spool_color_cache: Dict[int, str] = {}

    for slot, g in job_g.items():
        if g <= 0:
            continue
        slot_obj = st.slots.get(slot)
        spool_id = getattr(slot_obj, "spoolman_id", None) if slot_obj else None
        spool_id_norm = _spoolman_id_or_none(spool_id)
        color_hex = _normalize_color_hex(str(getattr(slot_obj, "color_hex", "") or ""))
        if spool_id_norm and spoolman_base:
            if spool_id_norm not in spool_color_cache:
                spool_color_cache[spool_id_norm] = ""
                try:
                    spool_data = _spoolman_get_spool(spoolman_base, spool_id_norm)
                    filament = spool_data.get("filament") or {}
                    spool_color_cache[spool_id_norm] = _normalize_color_hex(str(filament.get("color_hex") or ""))
                except Exception as e:
                    print(f"[MOON] ({printer_id}) spool color lookup failed for spool {spool_id_norm}: {e}")
            if spool_color_cache.get(spool_id_norm):
                color_hex = spool_color_cache[spool_id_norm]
        meters = max(0.0, float(job_mm.get(slot, 0.0) or 0.0) / 1000.0)
        total_grams += g
        total_meters += meters
        spools.append({
            "slot": slot,
            "spoolman_id": spool_id,
            "material": str(getattr(slot_obj, "material", "") or ""),
            "name": str(getattr(slot_obj, "name", "") or ""),
            "manufacturer": str(getattr(slot_obj, "manufacturer", "") or ""),
            "color_hex": color_hex,
            "grams": round(g, 2),
            "meters": round(meters, 4),
        })
        if spool_id:
            if _spoolman_mode() == "direct":
                _spoolman_report_usage(spool_id, g)
                print(f"[MOON] ({printer_id}) {reason}: slot {slot} → {g:.2f}g synced to Spoolman spool {spool_id}")
            else:
                print(f"[MOON] ({printer_id}) {reason}: slot {slot} → {g:.2f}g (moonraker mode, plugin tracks usage)")
        else:
            print(f"[MOON] ({printer_id}) {reason}: slot {slot} → {g:.2f}g (no Spoolman link, not synced)")
    if not job_g:
        print(f"[MOON] ({printer_id}) {reason}: no filament deltas recorded")

    # Persist lifetime stats for each slot that consumed filament this job
    now = _now()
    for slot, g in job_g.items():
        if g <= 0:
            continue
        stats = st.cfs_stats.get(slot) or SlotStats()
        stats.total_kg = round(stats.total_kg + g / 1000.0, 6)
        stats.total_meters = round(stats.total_meters + job_mm.get(slot, 0.0) / 1000.0, 4)
        stats.last_used_at = now
        st.cfs_stats[slot] = stats
    history = st.job_history if isinstance(st.job_history, list) else []
    history.append({
        "printer_id": printer_id,
        "job_name": job_name,
        "reason": reason,
        "started_at": started_ts,
        "ended_at": ended_ts,
        "spools": spools,
        "total_grams": round(total_grams, 2),
        "total_meters": round(total_meters, 4),
    })
    st.job_history = history[-50:]

    # Invalidate Spoolman percent cache so next WS parse picks up updated remaining_weight
    for slot in job_g:
        _spoolman_pct_refresh_at.setdefault(printer_id, {}).pop(slot, None)

    if any(g > 0 for g in job_g.values()) or bool(st.job_history):
        save_state(printer_id, st)

    _moon_job_track_slot_g[printer_id] = {}
    _moon_job_track_slot_mm[printer_id] = {}
    _moon_last_filament_mm[printer_id] = 0.0


def _resolve_tracking_slot(st: AppState) -> Optional[str]:
    """Three-tier priority: CFS active slot → SP slot (when no CFS slot active) → legacy active_slot."""
    # Prefer the live slot reported by WS when available.
    if st.cfs_active_slot and st.cfs_active_slot in st.slots:
        return st.cfs_active_slot

    # No active CFS slot — use direct spool input if it is present.
    # This also covers the case where CFS is connected but the printer is
    # currently feeding from the external spool holder (selected=0 on all CFS slots).
    cfs_slots = st.cfs_slots if isinstance(st.cfs_slots, dict) else {}
    sp_meta = cfs_slots.get(PRINTER_SPOOL_SLOT) if isinstance(cfs_slots, dict) else None
    sp_present = isinstance(sp_meta, dict) and bool(sp_meta.get("present", False))
    if sp_present and PRINTER_SPOOL_SLOT in st.slots:
        return PRINTER_SPOOL_SLOT

    # Final fallback: legacy active slot.
    if st.active_slot and st.active_slot in st.slots:
        return st.active_slot
    return None


async def moonraker_job_poll_loop(printer_id: str) -> None:
    """Poll Moonraker print_stats every 5s; attribute each filament delta to the active slot."""
    base = _moonraker_base_url(printer_id)
    if not base:
        print(f"[MOON] ({printer_id}) No printer URL configured — job poll loop not started.")
        return

    print(f"[MOON] ({printer_id}) Starting job poll loop against {base}")

    _ACTIVE_STATES = {"printing", "paused"}

    # Fetch and persist webcam URL once on startup
    webcam_url = _fetch_webcam_url(printer_id)
    if webcam_url:
        _st0 = load_state(printer_id)
        if _st0.moon_webcam_url != webcam_url:
            _st0.moon_webcam_url = webcam_url
            save_state(printer_id, _st0)
        print(f"[MOON] ({printer_id}) Webcam stream: {webcam_url}")

    while True:
        await asyncio.sleep(5.0)
        try:
            url = f"{base}/printer/objects/query?print_stats&extruder&heater_bed&display_status"
            data = _http_get_json(url, timeout=5.0)
            status = (data.get("result") or {}).get("status", {})
            ps = status.get("print_stats") or {}
            ext = status.get("extruder") or {}
            bed = status.get("heater_bed") or {}
            disp = status.get("display_status") or {}

            new_state = str(ps.get("state") or "").lower()
            filament_used_mm = float(ps.get("filament_used") or 0)

            # Update live status (temps + progress) — kept in memory only, not persisted
            _moon_live_status[printer_id] = {
                "moon_nozzle_temp": round(float(ext.get("temperature") or 0), 1),
                "moon_nozzle_target": round(float(ext.get("target") or 0), 1),
                "moon_bed_temp": round(float(bed.get("temperature") or 0), 1),
                "moon_bed_target": round(float(bed.get("target") or 0), 1),
                "moon_progress": round(float(disp.get("progress") or 0), 4),
                "moon_print_filename": str(ps.get("filename") or "").strip(),
                "moon_print_duration_s": int(float(ps.get("print_duration") or 0)),
                # Exposed so the frontend can distinguish "homing/meshing" from "extruding"
                "moon_filament_used_mm": round(filament_used_mm, 1),
            }
            job_name = str(ps.get("filename") or ps.get("job_name") or "").strip()

            prev = _moon_last_state.get(printer_id, "")
            _moon_last_state[printer_id] = new_state

            if new_state != prev:
                _st = load_state(printer_id)
                _st.moon_print_state = new_state
                save_state(printer_id, _st)

            if new_state in _ACTIVE_STATES and prev not in _ACTIVE_STATES:
                # Job started — reset trackers
                _moon_job_track_slot_g[printer_id] = {}
                _moon_job_track_slot_mm[printer_id] = {}
                _moon_last_filament_mm[printer_id] = filament_used_mm
                _moon_job_started_at[printer_id] = _now()
                _moon_job_name[printer_id] = job_name
                print(f"[MOON] ({printer_id}) State: {prev!r} → {new_state!r}; tracking filament deltas per active slot")
                # Moonraker mode: if printing from SP (no CFS slot active), activate SP's spool now.
                # CFS slot activations are handled in _parse_boxs_info via the WS stream.
                if _spoolman_mode() == "moonraker":
                    _st_job = load_state(printer_id)
                    if _resolve_tracking_slot(_st_job) == PRINTER_SPOOL_SLOT:
                        sp_spool_id = _st_job.slots[PRINTER_SPOOL_SLOT].spoolman_id if PRINTER_SPOOL_SLOT in _st_job.slots else None
                        _moonraker_set_active_spool(printer_id, sp_spool_id)

            elif new_state in _ACTIVE_STATES:
                if job_name:
                    _moon_job_name[printer_id] = job_name
                # Still printing/paused — attribute delta to currently active slot
                delta_mm = max(0.0, filament_used_mm - _moon_last_filament_mm.get(printer_id, 0.0))
                _moon_last_filament_mm[printer_id] = filament_used_mm
                if delta_mm > 0:
                    st = load_state(printer_id)
                    curr_slot = _resolve_tracking_slot(st)
                    if curr_slot and curr_slot in st.slots:
                        mat_str = str(getattr(st.slots[curr_slot], "material", "OTHER") or "OTHER")
                        g = mm_to_g(mat_str, delta_mm)
                        if g > 0:
                            job_g = _moon_job_track_slot_g.setdefault(printer_id, {})
                            job_mm = _moon_job_track_slot_mm.setdefault(printer_id, {})
                            job_g[curr_slot] = job_g.get(curr_slot, 0.0) + g
                            job_mm[curr_slot] = job_mm.get(curr_slot, 0.0) + delta_mm

            elif new_state in {"complete", "error", "cancelled"} and prev in _ACTIVE_STATES:
                # Capture any final delta, then flush accumulated grams to Spoolman
                delta_mm = max(0.0, filament_used_mm - _moon_last_filament_mm.get(printer_id, 0.0))
                if delta_mm > 0:
                    st = load_state(printer_id)
                    curr_slot = _resolve_tracking_slot(st)
                    if curr_slot and curr_slot in st.slots:
                        mat_str = str(getattr(st.slots[curr_slot], "material", "OTHER") or "OTHER")
                        g = mm_to_g(mat_str, delta_mm)
                        if g > 0:
                            job_g = _moon_job_track_slot_g.setdefault(printer_id, {})
                            job_mm = _moon_job_track_slot_mm.setdefault(printer_id, {})
                            job_g[curr_slot] = job_g.get(curr_slot, 0.0) + g
                            job_mm[curr_slot] = job_mm.get(curr_slot, 0.0) + delta_mm
                print(f"[MOON] ({printer_id}) State: {prev!r} → {new_state!r}; {filament_used_mm:.0f}mm total filament used")
                _moon_flush_to_spoolman(
                    printer_id,
                    f"Job {new_state}",
                    started_at=_moon_job_started_at.get(printer_id),
                    ended_at=_now(),
                    job_name=_moon_job_name.get(printer_id, job_name),
                )
                _moon_job_started_at.pop(printer_id, None)
                _moon_job_name.pop(printer_id, None)

        except Exception:
            # Network errors are expected when printer is off — don't log verbosely
            pass


app = FastAPI(title="CFSync", version="0.1.1")

# Allow the Fluidd panel bookmarklet to fetch from a different origin (local network only)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    """Disable browser caching for /static assets.

    This project is frequently updated in-place on the host. Some browsers keep
    serving an older /static/app.js via 304 responses unless caching is
    explicitly disabled. Prevent that.
    """
    response = await call_next(request)
    path = request.url.path or ""
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Static UI on /
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def _startup():
    _ensure_data_files()
    printer_ids = [str((p or {}).get("id") or "") for p in (load_config().get("printers") or []) if (p or {}).get("id")]
    if not printer_ids:
        print("[BOOT] No printers configured — waiting for data/config.json")
    for pid in printer_ids:
        asyncio.create_task(printer_ws_loop(pid))
        asyncio.create_task(moonraker_job_poll_loop(pid))


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# --- Public API ---
@app.get("/api/state", response_model=AppState)
def api_state(printer_id: Optional[str] = None):
    pid = _resolve_printer_id(printer_id, allow_unknown=printer_id is None)
    return load_state(pid)


def _ui_state_dict(state: AppState) -> dict:
    """Convert internal AppState to the UI payload the static frontend expects."""
    d = _model_dump(state)
    slots_in = d.get("slots", {}) or {}
    slots_out: Dict[str, dict] = {}
    for slot_id, sd in slots_in.items():
        if not isinstance(sd, dict):
            sd = _model_dump(sd)
        out = dict(sd)
        if "color_hex" in out and "color" not in out:
            out["color"] = out.pop("color_hex")
        if "manufacturer" in out and "vendor" not in out:
            out["vendor"] = out.get("manufacturer", "")
        slots_out[slot_id] = out
    d["slots"] = slots_out

    d.setdefault("printer_connected", False)
    d.setdefault("printer_last_error", "")
    d.setdefault("cfs_connected", False)
    d.setdefault("cfs_last_update", 0.0)
    d.setdefault("cfs_active_slot", None)
    d.setdefault("cfs_slots", {})
    d.setdefault("cfs_stats", {})
    d.setdefault("cfs_env_history", {})
    d.setdefault("job_history", [])
    d["job_history"] = _ui_hydrate_job_history_colors(d["job_history"])
    d["spoolman_configured"] = bool(_spoolman_base_url())
    d["spoolman_url"] = _spoolman_base_url()
    d["spoolman_mode"] = _spoolman_mode()

    return d


# --- UI API (static frontend uses /api/ui/* and expects {"result": ...}) ---
@app.get("/api/ui/state", response_model=ApiResponse)
def api_ui_state() -> ApiResponse:
    printers_out = []
    for pid in _all_printer_ids():
        st = load_state(pid)
        d = _ui_state_dict(st)
        d["printer_id"] = pid
        d.update(_moon_live_status.get(pid, {}))  # inject live temps/progress
        printers_out.append({"id": pid, "state": d})
    return ApiResponse(result={
        "printers": printers_out,
        "spoolman_configured": bool(_spoolman_base_url()),
        "spoolman_url": _spoolman_base_url(),
    })


@app.get("/api/printers")
def api_printers():
    printers = []
    for pid in _all_printer_ids():
        st = load_state(pid)
        printers.append({
            "id": pid,
            "address": _printer_address(pid),
            "name": st.printer_name,
            "firmware": st.printer_firmware,
            "connected": st.printer_connected,
            "cfs_connected": st.cfs_connected,
            "last_error": st.printer_last_error,
        })
    return {"printers": printers}


@app.post("/api/select_slot", response_model=AppState)
def api_select_slot(req: SelectSlotRequest):
    pid = _resolve_printer_id(req.printer_id, allow_unknown=req.printer_id is None)
    state = load_state(pid)
    if req.slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")
    state.active_slot = req.slot
    save_state(pid, state)
    return state


@app.post("/api/ui/select_slot", response_model=ApiResponse)
def api_ui_select_slot(req: SelectSlotRequest) -> ApiResponse:
    state = api_select_slot(req)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/set_auto", response_model=AppState)
def api_set_auto(req: SetAutoRequest):
    pid = _resolve_printer_id(req.printer_id, allow_unknown=req.printer_id is None)
    state = load_state(pid)
    state.auto_mode = bool(req.enabled)
    save_state(pid, state)
    return state


@app.post("/api/ui/set_auto", response_model=ApiResponse)
def api_ui_set_auto(req: SetAutoRequest) -> ApiResponse:
    state = api_set_auto(req)
    return ApiResponse(result=_ui_state_dict(state))


@app.patch("/api/slots/{slot}", response_model=AppState)
def api_update_slot(slot: str, req: UpdateSlotRequest):
    pid = _resolve_printer_id(req.printer_id, allow_unknown=req.printer_id is None)
    state = load_state(pid)
    if slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")

    s = state.slots[slot]
    update = _req_dump(req, exclude_unset=True)
    for k, v in update.items():
        if hasattr(s, k):
            setattr(s, k, v)

    state.slots[slot] = s
    save_state(pid, state)
    return state


@app.post("/api/ui/slot/update", response_model=ApiResponse)
def api_ui_slot_update(req: UiSlotUpdateRequest) -> ApiResponse:
    pid = _resolve_printer_id(req.printer_id, allow_unknown=req.printer_id is None)
    state = load_state(pid)
    slot = req.slot
    if slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")

    s = state.slots[slot]
    upd = _req_dump(req, exclude_unset=True)

    # UI uses 'color' but internal uses 'color_hex'
    if "color" in upd:
        s.color_hex = upd.pop("color")

    upd.pop("slot", None)

    # vendor -> manufacturer
    if "vendor" in upd and upd.get("vendor") is not None:
        upd["manufacturer"] = upd.pop("vendor")

    for k, v in upd.items():
        if v is None:
            continue
        if hasattr(s, k):
            setattr(s, k, v)

    state.slots[slot] = s
    save_state(pid, state)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/ui/spool/set_start", response_model=ApiResponse)
def api_ui_spool_set_start(req: UiSpoolSetStartRequest) -> ApiResponse:
    """Roll change: increment epoch and auto-unlink Spoolman spool."""
    pid = _resolve_printer_id(req.printer_id, allow_unknown=req.printer_id is None)
    state = load_state(pid)
    slot = req.slot
    if slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")

    s = state.slots[slot]
    # New roll => new epoch (hides old history in Spoolman status, triggers auto-unlink)
    try:
        s.spool_epoch = int(getattr(s, "spool_epoch", 0) or 0) + 1
    except Exception:
        s.spool_epoch = 1
    # Roll change auto-unlinks Spoolman spool
    s.spoolman_id = None
    state.slots[slot] = s
    # Reset WS length baseline so next snapshot doesn't trigger a false delta
    state.ws_slot_length_m.pop(slot, None)
    # Clear RFID/state cache so re-inserting any spool triggers auto-link again
    _ws_last_rfid.setdefault(pid, {}).pop(slot, None)
    _ws_last_state.setdefault(pid, {}).pop(slot, None)
    _ws_last_fingerprint.setdefault(pid, {}).pop(slot, None)
    save_state(pid, state)
    return ApiResponse(result=_ui_state_dict(state))


# --- Spoolman integration endpoints ---

@app.get("/api/ui/spoolman/spools")
def api_ui_spoolman_spools(slot: str = "1A", printer_id: Optional[str] = None):
    """Fetch available Spoolman spools, sorted by match quality for the given slot."""
    base = _spoolman_base_url()
    if not base:
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")

    pid = _resolve_printer_id(printer_id, allow_unknown=printer_id is None)
    state = load_state(pid)
    s = state.slots.get(slot)
    cfs_slot = state.cfs_slots.get(slot) if isinstance(state.cfs_slots, dict) else None
    has_cfs_snapshot = isinstance(state.cfs_slots, dict) and bool(state.cfs_slots)
    slot_present = True
    if isinstance(cfs_slot, dict):
        slot_present = bool(cfs_slot.get("present", True))
    elif has_cfs_snapshot:
        slot_present = False
    slot_material = (getattr(s, "material", "") or "").upper() if (s and slot_present) else ""
    slot_color = (getattr(s, "color_hex", "") or "").lower() if (s and slot_present) else ""

    try:
        raw = _spoolman_get_spools(base)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Spoolman unreachable: {e}")

    spools = []
    for sp in raw:
        filament = sp.get("filament") or {}
        mat = (filament.get("material") or "").upper()
        color_hex = (filament.get("color_hex") or "").lower()
        name = filament.get("name") or ""
        vendor = (filament.get("vendor") or {}).get("name", "")
        remaining = sp.get("remaining_weight")

        # Score: lower is better. Same material gets a big bonus.
        score = 0
        if mat == slot_material:
            score -= 1000
        if slot_color and color_hex:
            score += _color_distance(slot_color, color_hex)

        spools.append({
            "id": sp.get("id"),
            "filament_name": name,
            "vendor": vendor,
            "material": mat,
            "color_hex": color_hex,
            "remaining_weight": remaining,
            "_score": score,
        })

    spools.sort(key=lambda x: x["_score"])
    for sp in spools:
        del sp["_score"]

    return {"spools": spools, "slot": slot, "printer_id": pid}


@app.post("/api/ui/spoolman/link", response_model=ApiResponse)
def api_ui_spoolman_link(req: SpoolmanLinkRequest) -> ApiResponse:
    """Link a Spoolman spool to a CFS slot. Imports remaining_weight as local reference."""
    base = _spoolman_base_url()
    if not base:
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")

    pid = _resolve_printer_id(req.printer_id, allow_unknown=req.printer_id is None)
    state = load_state(pid)
    slot = req.slot
    if slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")

    try:
        sp = _spoolman_get_spool(base, req.spoolman_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Spoolman unreachable: {e}")

    filament = sp.get("filament") or {}

    s = state.slots[slot]
    s.spoolman_id = req.spoolman_id

    # Import spool metadata from Spoolman
    mat_raw = (filament.get("material") or "").strip().upper()
    if mat_raw in ("PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC"):
        s.material = mat_raw
    color_hex = (filament.get("color_hex") or "").strip()
    if color_hex and len(color_hex) == 7 and color_hex.startswith("#"):
        s.color_hex = color_hex
    fname = (filament.get("name") or "").strip()
    if fname:
        s.name = fname
    vendor_name = ((filament.get("vendor") or {}).get("name") or "").strip()
    if vendor_name:
        s.manufacturer = vendor_name

    state.slots[slot] = s
    save_state(pid, state)

    # Moonraker mode: notify printer when a spool is linked on the currently active slot.
    # SP is never set as cfs_active_slot by firmware, so check it separately.
    if _spoolman_mode() == "moonraker":
        sp_meta_lnk = state.cfs_slots.get(PRINTER_SPOOL_SLOT) if isinstance(state.cfs_slots, dict) else None
        sp_present_lnk = isinstance(sp_meta_lnk, dict) and bool(sp_meta_lnk.get("present"))
        if state.cfs_active_slot == slot or (slot == PRINTER_SPOOL_SLOT and sp_present_lnk and not state.cfs_active_slot):
            _moonraker_set_active_spool(pid, req.spoolman_id)

    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/ui/spoolman/unlink", response_model=ApiResponse)
def api_ui_spoolman_unlink(req: SpoolmanUnlinkRequest) -> ApiResponse:
    """Clear Spoolman link on a slot. Local tracking is unaffected."""
    pid = _resolve_printer_id(req.printer_id, allow_unknown=req.printer_id is None)
    state = load_state(pid)
    slot = req.slot
    if slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")

    if _spoolman_mode() == "moonraker":
        sp_meta_ulnk = state.cfs_slots.get(PRINTER_SPOOL_SLOT) if isinstance(state.cfs_slots, dict) else None
        sp_present_ulnk = isinstance(sp_meta_ulnk, dict) and bool(sp_meta_ulnk.get("present"))
        if state.cfs_active_slot == slot or (slot == PRINTER_SPOOL_SLOT and sp_present_ulnk and not state.cfs_active_slot):
            _moonraker_set_active_spool(pid, None)
    state.slots[slot].spoolman_id = None
    save_state(pid, state)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/ui/jobs/reallocate_spool", response_model=ApiResponse)
def api_ui_jobs_reallocate_spool(req: JobReallocateSpoolRequest) -> ApiResponse:
    """Relink a completed job spool usage entry to another Spoolman spool."""
    base = _spoolman_base_url()
    if not base:
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")

    pid = _resolve_printer_id(req.printer_id, allow_unknown=req.printer_id is None)
    state = load_state(pid)
    history = state.job_history if isinstance(state.job_history, list) else []

    target_spool = None
    req_slot = str(req.slot)
    req_ended_at = float(req.ended_at)
    for job in reversed(history):
        if not isinstance(job, dict):
            continue
        try:
            ended_at = float(job.get("ended_at") or 0.0)
        except Exception:
            ended_at = 0.0
        if abs(ended_at - req_ended_at) > 1.0:
            continue
        spools = job.get("spools") or []
        if not isinstance(spools, list):
            continue
        for sp in spools:
            if not isinstance(sp, dict):
                continue
            if str(sp.get("slot") or "") == req_slot:
                target_spool = sp
                break
        if target_spool:
            break

    if target_spool is None:
        raise HTTPException(status_code=404, detail="Job spool entry not found")

    try:
        new_spool = _spoolman_get_spool(base, req.spoolman_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Spoolman unreachable: {e}")

    grams = max(0.0, float(target_spool.get("grams") or 0.0))
    old_spool_id = _spoolman_id_or_none(target_spool.get("spoolman_id"))
    new_spool_id = int(req.spoolman_id)

    # Move historical usage between Spoolman spools:
    # - Remove job usage from the new linked spool
    # - Add that usage back to the old linked spool (if there was one)
    if grams > 0 and old_spool_id != new_spool_id:
        old_remaining = None
        old_target = None
        if old_spool_id:
            try:
                old_spool = _spoolman_get_spool(base, old_spool_id)
                old_remaining = _spoolman_remaining_weight(old_spool)
                old_target = old_remaining + grams
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Failed to read previous spool #{old_spool_id}: {e}")
        new_remaining = _spoolman_remaining_weight(new_spool)
        new_target = max(0.0, new_remaining - grams)
        try:
            _spoolman_set_remaining_weight(base, new_spool_id, new_target)
            if old_spool_id and old_target is not None:
                _spoolman_set_remaining_weight(base, old_spool_id, old_target)
        except Exception as e:
            # Best-effort rollback so we do not leave usage in a half-moved state.
            try:
                _spoolman_set_remaining_weight(base, new_spool_id, new_remaining)
            except Exception:
                pass
            raise HTTPException(status_code=502, detail=f"Failed to move spool usage: {e}")

    filament = new_spool.get("filament") or {}
    target_spool["spoolman_id"] = new_spool_id
    if filament.get("material") is not None:
        target_spool["material"] = str(filament.get("material") or "").upper()
    if filament.get("name") is not None:
        target_spool["name"] = str(filament.get("name") or "")
    if filament.get("vendor") is not None:
        target_spool["manufacturer"] = str((filament.get("vendor") or {}).get("name") or "")
    target_spool["color_hex"] = _normalize_color_hex(str(filament.get("color_hex") or ""))

    state.job_history = history[-50:]
    save_state(pid, state)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/ui/set_spoolman_mode", response_model=ApiResponse)
def api_ui_set_spoolman_mode(req: SetSpoolmanModeRequest) -> ApiResponse:
    """Switch Spoolman sync mode between 'direct' and 'moonraker'."""
    if req.mode not in ("direct", "moonraker"):
        raise HTTPException(status_code=400, detail="mode must be 'direct' or 'moonraker'")
    cfg = load_config()
    cfg["spoolman_mode"] = req.mode
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    print(f"[CONFIG] spoolman_mode set to {req.mode!r}")
    pid = _default_printer_id()
    state = load_state(pid)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/ui/set_spoolman_url", response_model=ApiResponse)
def api_ui_set_spoolman_url(req: SetSpoolmanUrlRequest) -> ApiResponse:
    """Set (or clear) the Spoolman server URL."""
    url = req.url.strip().rstrip("/")
    cfg = load_config()
    cfg["spoolman_url"] = url
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    print(f"[CONFIG] spoolman_url set to {url!r}")
    pid = _default_printer_id()
    state = load_state(pid)
    return ApiResponse(result=_ui_state_dict(state))


@app.get("/api/ui/spoolman/spool_detail")
def api_ui_spoolman_spool_detail(slot: str = "1A", printer_id: Optional[str] = None):
    """Proxy Spoolman spool status for a given CFS slot.

    Returns {"linked": bool, "slot": str, "spool": dict|null, "error": str|null}.
    Never raises HTTP 502 — Spoolman unavailability is returned as a structured error
    so the frontend can degrade gracefully.
    """
    pid = _resolve_printer_id(printer_id, allow_unknown=printer_id is None)
    state = load_state(pid)
    slot_obj = state.slots.get(slot)
    if slot_obj is None:
        raise HTTPException(status_code=404, detail="Unknown slot")

    spool_id = getattr(slot_obj, "spoolman_id", None)
    if not spool_id:
        return {"linked": False, "slot": slot, "spool": None, "error": None, "printer_id": pid}

    base = _spoolman_base_url()
    if not base:
        return {"linked": True, "slot": slot, "spool": None, "error": "not_configured", "printer_id": pid}

    try:
        sp = _spoolman_get_spool(base, spool_id)
        return {"linked": True, "slot": slot, "spool": sp, "error": None, "printer_id": pid}
    except Exception as e:
        return {"linked": True, "slot": slot, "spool": None, "error": "unreachable", "printer_id": pid}


@app.post("/api/ui/set_color", response_model=ApiResponse)
def api_ui_set_color(req: UiSetColorRequest) -> ApiResponse:
    pid = _resolve_printer_id(req.printer_id, allow_unknown=req.printer_id is None)
    state = load_state(pid)
    if req.slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")
    state.slots[req.slot].color_hex = req.color
    save_state(pid, state)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/feed")
def api_feed(req: FeedRequest):
    adapter_feed(req.mm)
    return {"ok": True}


@app.post("/api/ui/feed", response_model=ApiResponse)
def api_ui_feed(req: FeedRequest) -> ApiResponse:
    api_feed(req)
    return ApiResponse(result={"ok": True})


@app.post("/api/retract")
def api_retract(req: RetractRequest):
    adapter_retract(req.mm)
    return {"ok": True}


@app.post("/api/ui/retract", response_model=ApiResponse)
def api_ui_retract(req: RetractRequest) -> ApiResponse:
    api_retract(req)
    return ApiResponse(result={"ok": True})


@app.get("/api/ui/help", response_model=ApiResponse)
def api_ui_help(lang: str = "de") -> ApiResponse:
    if lang == "en":
        text = (
            "Click a slot to set it as active.\n"
            "Set printer_urls (or printers) in data/config.json to your printer IPs to enable live CFS slot sync via WebSocket.\n"
            "Link a Spoolman spool to a slot to track filament consumption automatically."
        )
    else:
        text = (
            "Klick einen Slot, um ihn aktiv zu setzen.\n"
            "Trage printer_urls (oder printers) in data/config.json mit den IPs deiner Drucker ein, um die CFS-Slots per WebSocket zu synchronisieren.\n"
            "Verknüpfe einen Spoolman-Spool mit einem Slot, um den Filamentverbrauch automatisch zu verfolgen."
        )
    return ApiResponse(result={"text": text})


# Health
@app.get("/api/health")
def api_health():
    return {"ok": True, "ts": _now()}


def default_state() -> AppState:
    """Safe defaults if state.json is missing/broken.

    Must always include all 4x4 CFS slots and the direct printer spool input so
    the UI never crashes, even if the state file is corrupted.
    """
    slots: Dict[str, SlotState] = {}
    for sid in DEFAULT_SLOTS:
        slots[sid] = SlotState(slot=sid, material="OTHER", color_hex="#00aaff")
    slots[PRINTER_SPOOL_SLOT] = SlotState(slot=PRINTER_SPOOL_SLOT, material="OTHER", color_hex="#00aaff")

    # Sensible demo defaults for Box 2 (matches the UI screenshot vibe)
    slots["2A"].material = "ABS"
    slots["2A"].color_hex = "#4b0082"  # indigo-ish

    return AppState(
        active_slot="2A",
        auto_mode=False,
        updated_at=_now(),
        slots=slots,  # type: ignore[arg-type]
        printer_connected=False,
        printer_last_error="",
        cfs_connected=False,
        cfs_last_update=0.0,
        cfs_active_slot=None,
        cfs_slots={},
        cfs_env_history={},
    )


def default_multi_state() -> MultiAppState:
    printers: Dict[str, AppState] = {}
    for p in load_config().get("printers") or []:
        pid = str((p or {}).get("id") or "")
        if pid:
            printers[pid] = default_state()
    return MultiAppState(printers=printers, updated_at=_now())
