"""
settings_manager.py — Управление персистентными настройками MRKT Bot.

Сохраняет настройки в persistent JSON файл, защищенный от перезаписи при git pull
и перезапуске Docker-контейнеров.
Приоритет путей:
1. data/settings.json (если папка data существует или монтируется)
2. logs/settings.json (если папка logs смонтирована)
3. settings.json в корне проекта
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("scanner")

DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_buy": False,
    "filter_by_balance": False,
    "min_ton_diff": 2.5,
    "cheap_price_threshold": 3.0,
    "max_gift_price_ton": 0.0,
    "min_margin_pct": 5.0,
    "eval_mode": "tiered",
    "min_turnover_ratio": 0.0,
    "scan_interval": 0.8,
    "notify_categories": {
        "BLACK": "autobuy",
        "CHEAP": "autobuy",
        "NFT": "autobuy",
        "LOW_ID": "autobuy",
    },
    "primary_token": "",
    "use_direct": False,
}


def normalize_notify_categories(raw_cats: Any) -> dict[str, str]:
    """Нормализует категории к строкам: 'autobuy' | 'notify' | 'off'."""
    res = dict(DEFAULT_SETTINGS["notify_categories"])
    if not isinstance(raw_cats, dict):
        return res
    for k, v in raw_cats.items():
        if isinstance(v, bool):
            res[k] = "autobuy" if v else "off"
        elif isinstance(v, str) and v.lower() in ("autobuy", "notify", "off"):
            res[k] = v.lower()
        elif v in (1, "1", "true"):
            res[k] = "autobuy"
        elif v in (0, "0", "false"):
            res[k] = "off"
        else:
            res[k] = "autobuy"
    return res


def get_settings_path() -> Path:
    """Определяет наиболее подходящий путь для settings.json."""
    # 1. Если существует папка data (или смонтирована)
    p_data = Path("data")
    if p_data.is_dir():
        return p_data / "settings.json"

    # 2. Если уже есть settings.json в data, возвращаем его
    if (p_data / "settings.json").exists():
        return p_data / "settings.json"

    # 3. Если есть папка logs (которая смонтирована в Docker)
    p_logs = Path("logs")
    if (p_logs / "settings.json").exists():
        return p_logs / "settings.json"

    # 4. Если в корне есть settings.json
    if Path("settings.json").exists():
        return Path("settings.json")

    # По умолчанию создаем в data/ если удастся создать, иначе logs/
    try:
        p_data.mkdir(parents=True, exist_ok=True)
        return p_data / "settings.json"
    except Exception:
        pass

    if p_logs.is_dir():
        return p_logs / "settings.json"

    return Path("settings.json")


def load_settings() -> dict[str, Any]:
    """
    Загружает настройки из доступного settings.json.
    Если файла нет или он повреждён, возвращает пустой словарь.
    """
    candidate_paths = [
        Path("data/settings.json"),
        Path("logs/settings.json"),
        Path("settings.json"),
    ]

    for path in candidate_paths:
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        if "notify_categories" in data:
                            data["notify_categories"] = normalize_notify_categories(data["notify_categories"])
                        if "max_gift_price_ton" in data:
                            try:
                                data["max_gift_price_ton"] = float(data["max_gift_price_ton"])
                            except (ValueError, TypeError):
                                data["max_gift_price_ton"] = 0.0
                        if "min_margin_pct" in data:
                            try:
                                data["min_margin_pct"] = float(data["min_margin_pct"])
                            except (ValueError, TypeError):
                                data["min_margin_pct"] = 5.0
                        if "eval_mode" in data:
                            em = str(data["eval_mode"]).lower()
                            data["eval_mode"] = em if em in ("tiered", "fixed") else "tiered"
                        log.info("Загружены персистентные настройки из %s", path)
                        return data
            except Exception as e:
                log.warning("Не удалось прочитать настройки из %s: %s", path, e)

    return {}


def save_settings(state_or_dict: Any) -> bool:
    """
    Атомарно сохраняет настройки в JSON файл.
    Принимает либо ScannerState, либо dict.
    """
    if hasattr(state_or_dict, "__dict__"):
        state = state_or_dict
        pool = getattr(state, "pool", None)
        prim_tok = pool.primary_token if pool else ""
        raw_cats = getattr(state, "notify_categories", DEFAULT_SETTINGS["notify_categories"])
        norm_cats = normalize_notify_categories(raw_cats)
        em = str(getattr(state, "eval_mode", "tiered")).lower()
        data = {
            "auto_buy": bool(getattr(state, "auto_buy", False)),
            "filter_by_balance": bool(getattr(state, "filter_by_balance", False)),
            "min_ton_diff": float(getattr(state, "min_ton_diff", 2.5)),
            "cheap_price_threshold": float(getattr(state, "cheap_price_threshold", 3.0)),
            "max_gift_price_ton": float(getattr(state, "max_gift_price_ton", 0.0)),
            "min_margin_pct": float(getattr(state, "min_margin_pct", 5.0)),
            "eval_mode": em if em in ("tiered", "fixed") else "tiered",
            "min_turnover_ratio": float(getattr(state, "min_turnover_ratio", 0.0)),
            "scan_interval": float(getattr(state, "scan_interval", 0.5)),
            "notify_categories": norm_cats,
            "primary_token": str(prim_tok or ""),
            "use_direct": bool(getattr(state, "use_direct", False)),
        }
    elif isinstance(state_or_dict, dict):
        data = dict(state_or_dict)
        if "notify_categories" in data:
            data["notify_categories"] = normalize_notify_categories(data["notify_categories"])
        if "max_gift_price_ton" in data:
            try:
                data["max_gift_price_ton"] = float(data["max_gift_price_ton"])
            except (ValueError, TypeError):
                data["max_gift_price_ton"] = 0.0
        if "min_margin_pct" in data:
            try:
                data["min_margin_pct"] = float(data["min_margin_pct"])
            except (ValueError, TypeError):
                data["min_margin_pct"] = 5.0
        if "eval_mode" in data:
            em = str(data["eval_mode"]).lower()
            data["eval_mode"] = em if em in ("tiered", "fixed") else "tiered"
    else:
        log.error("save_settings: недопустимый тип %s", type(state_or_dict))
        return False

    target_path = get_settings_path()
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Атомарная запись через временный файл в той же директории
        temp_fd, temp_file = tempfile.mkstemp(
            dir=target_path.parent,
            prefix="settings_",
            suffix=".tmp",
        )
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, target_path)
        log.debug("Настройки успешно сохранены в %s", target_path)
        return True
    except Exception as e:
        log.error("Ошибка сохранения настроек в %s: %s", target_path, e)
        return False
