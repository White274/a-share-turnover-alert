#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股 10:00 / 10:15 定点成交额采集器。

数据源：
- 东方财富 5 分钟指数 K 线 JSON 接口
- 沪市口径：1.000001（上证综指）
- 深市口径：0.399001（深证成指）
- 成交额字段：f57（每根 5 分钟 K 线区间成交额，单位：元）

本脚本只接受完整、已封口的目标时点 K 线，不使用新闻、实时快照、
估算、插值或反推。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
BASE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
MARKETS = {
    "shanghai": {"secid": "1.000001", "label": "沪市（上证综指口径）"},
    "shenzhen": {"secid": "0.399001", "label": "深市（深证成指口径）"},
}
TARGETS = ("10:00", "10:15")
EXPECTED_TIMES = {
    "10:00": ("09:35", "09:40", "09:45", "09:50", "09:55", "10:00"),
    "10:15": (
        "09:35", "09:40", "09:45", "09:50", "09:55",
        "10:00", "10:05", "10:10", "10:15",
    ),
}
REPORTS_DIR = Path("reports")
DATA_DIR = Path("data")
HISTORY_CSV = DATA_DIR / "history.csv"


@dataclass(frozen=True)
class Bar:
    stamp: datetime
    amount_yuan: Decimal


def now_cn() -> datetime:
    return datetime.now(TZ)


def build_url(secid: str, limit: int = 1000) -> str:
    params = {
        "secid": secid,
        "klt": "5",
        "fqt": "1",
        "lmt": str(limit),
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    return f"{BASE_URL}?{urlencode(params)}"


def fetch_json(url: str, attempts: int = 5, timeout: int = 20) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
        ),
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json,text/plain,*/*",
    }
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="strict")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("接口返回内容不是JSON对象")
            if payload.get("data") is None:
                raise ValueError(f"接口data为空：{payload.get('message') or payload.get('msg')}")
            return payload
        except Exception as exc:  # 网络、HTTP、JSON均统一重试
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 12))
    raise RuntimeError(f"行情接口连续{attempts}次请求失败：{last_error}") from last_error


def parse_bars(payload: dict) -> list[Bar]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("接口data格式异常")
    klines = data.get("klines")
    if not isinstance(klines, list) or not klines:
        raise ValueError("接口未返回K线")

    bars: list[Bar] = []
    for raw in klines:
        if not isinstance(raw, str):
            continue
        parts = raw.split(",")
        if len(parts) < 7:
            continue
        try:
            stamp = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            amount = Decimal(parts[6])
        except (ValueError, InvalidOperation):
            continue
        if amount < 0:
            continue
        bars.append(Bar(stamp=stamp, amount_yuan=amount))

    if not bars:
        raise ValueError("没有可解析的5分钟K线")
    bars.sort(key=lambda item: item.stamp)
    return bars


def load_market_bars() -> dict[str, list[Bar]]:
    result: dict[str, list[Bar]] = {}
    for key, meta in MARKETS.items():
        payload = fetch_json(build_url(meta["secid"]))
        result[key] = parse_bars(payload)
    return result


def index_bars_by_date(bars: Iterable[Bar]) -> dict[date, dict[str, Decimal]]:
    indexed: dict[date, dict[str, Decimal]] = {}
    for bar in bars:
        day = bar.stamp.date()
        hhmm = bar.stamp.strftime("%H:%M")
        indexed.setdefault(day, {})[hhmm] = bar.amount_yuan
    return indexed


def day_amount(
    indexed: dict[date, dict[str, Decimal]],
    day: date,
    target: str,
) -> tuple[Decimal | None, list[str]]:
    day_rows = indexed.get(day, {})
    required = EXPECTED_TIMES[target]
    missing = [hhmm for hhmm in required if hhmm not in day_rows]
    if missing:
        return None, missing
    return sum((day_rows[hhmm] for hhmm in required), Decimal("0")), []


def complete_total(
    indexed_by_market: dict[str, dict[date, dict[str, Decimal]]],
    day: date,
    target: str,
) -> tuple[dict[str, Decimal] | None, dict[str, list[str]]]:
    values: dict[str, Decimal] = {}
    missing: dict[str, list[str]] = {}
    for market in ("shanghai", "shenzhen"):
        amount, missed = day_amount(indexed_by_market[market], day, target)
        if amount is None:
            missing[market] = missed
        else:
            values[market] = amount
    if missing:
        return None, missing
    values["total"] = values["shanghai"] + values["shenzhen"]
    return values, {}


def get_previous_complete_days(
    indexed_by_market: dict[str, dict[date, dict[str, Decimal]]],
    current_day: date,
    target: str,
    count: int = 5,
) -> list[tuple[date, dict[str, Decimal]]]:
    all_days = sorted(
        set(indexed_by_market["shanghai"]) & set(indexed_by_market["shenzhen"]),
        reverse=True,
    )
    result: list[tuple[date, dict[str, Decimal]]] = []
    for day in all_days:
        if day >= current_day:
            continue
        values, _ = complete_total(indexed_by_market, day, target)
        if values is not None:
            result.append((day, values))
        if len(result) == count:
            break
    return result


def yuan_to_yi(value: Decimal) -> Decimal:
    return (value / Decimal("100000000")).quantize(Decimal("0.01"))


def pct_change(current: Decimal, base: Decimal) -> Decimal | None:
    if base == 0:
        return None
    return ((current - base) / base * Decimal("100")).quantize(Decimal("0.01"))


def signed_number(value: Decimal | None, suffix: str = "") -> str:
    if value is None:
        return "无法计算"
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value}{suffix}"


def classify(pct: Decimal | None) -> str:
    if pct is None:
        return "样本不足，暂不分类"
    if pct >= Decimal("15"):
        return "明显放量"
    if pct >= Decimal("5"):
        return "温和放量"
    if pct > Decimal("-5"):
        return "基本持平"
    if pct > Decimal("-15"):
        return "温和缩量"
    return "明显缩量"


def direction(pct: Decimal | None) -> str:
    if pct is None:
        return "未知"
    if pct > 0:
        return "增加"
    if pct < 0:
        return "减少"
    return "持平"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def write_json(path: Path, payload: dict) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def update_history(record: dict) -> None:
    HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "trade_date",
        "target_time",
        "generated_at",
        "shanghai_amount_yi",
        "shenzhen_amount_yi",
        "total_amount_yi",
        "previous_day",
        "previous_day_total_yi",
        "previous_day_change_pct",
        "five_day_average_yi",
        "five_day_change_pct",
        "classification",
        "status",
    ]
    rows: list[dict[str, str]] = []
    if HISTORY_CSV.exists():
        with HISTORY_CSV.open("r", encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))

    key = (record["trade_date"], record["target_time"])
    rows = [
        row for row in rows
        if (row.get("trade_date"), row.get("target_time")) != key
    ]
    rows.append({name: str(record.get(name, "")) for name in fieldnames})
    rows.sort(key=lambda row: (row["trade_date"], row["target_time"]))

    with HISTORY_CSV.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_report(
    trade_day: date,
    target: str,
    current: dict[str, Decimal],
    previous_days: list[tuple[date, dict[str, Decimal]]],
    generated_at: datetime,
) -> tuple[str, dict]:
    sh_yi = yuan_to_yi(current["shanghai"])
    sz_yi = yuan_to_yi(current["shenzhen"])
    total_yi = yuan_to_yi(current["total"])

    previous_day_text = "样本不足"
    previous_total_yi: Decimal | None = None
    previous_pct: Decimal | None = None
    previous_diff_yi: Decimal | None = None
    if previous_days:
        previous_day, previous_values = previous_days[0]
        previous_total_yi = yuan_to_yi(previous_values["total"])
        previous_diff_yi = (total_yi - previous_total_yi).quantize(Decimal("0.01"))
        previous_pct = pct_change(total_yi, previous_total_yi)
        previous_day_text = previous_day.isoformat()

    five_avg_yi: Decimal | None = None
    five_diff_yi: Decimal | None = None
    five_pct: Decimal | None = None
    if len(previous_days) == 5:
        five_avg_yuan = sum(
            (values["total"] for _, values in previous_days), Decimal("0")
        ) / Decimal("5")
        five_avg_yi = yuan_to_yi(five_avg_yuan)
        five_diff_yi = (total_yi - five_avg_yi).quantize(Decimal("0.01"))
        five_pct = pct_change(total_yi, five_avg_yi)

    label = classify(five_pct)
    directions_consistent = (
        previous_pct is not None
        and five_pct is not None
        and direction(previous_pct) == direction(five_pct)
    )
    consistency_text = (
        "较上一交易日与较五日均值方向一致"
        if directions_consistent
        else "较上一交易日与较五日均值方向不一致或样本不足"
    )

    historical_lines = []
    for day, values in previous_days:
        historical_lines.append(
            f"- {day.isoformat()}：{yuan_to_yi(values['total'])}亿元（完整）"
        )
    if len(previous_days) < 5:
        historical_lines.append(
            f"- 历史完整样本不足：仅取得{len(previous_days)}/5个交易日"
        )

    report = f"""# A股{target}量能报告

- 交易日期：{trade_day.isoformat()}
- 目标时点：{target}
- 数据状态：success
- 实际生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）
- 沪市成交额（上证综指口径）：{sh_yi}亿元
- 深市成交额（深证成指口径）：{sz_yi}亿元
- 沪深两市合计成交额：{total_yi}亿元
- 上一完整交易日：{previous_day_text}
- 较上一交易日同期：{signed_number(previous_diff_yi, '亿元')}，{signed_number(previous_pct, '%')}
- 前五个完整交易日同期均值：{str(five_avg_yi) + '亿元' if five_avg_yi is not None else '样本不足'}
- 较五日同期均值：{signed_number(five_diff_yi, '亿元')}，{signed_number(five_pct, '%')}
- 量能结论：{label}；{consistency_text}

## 前五个交易日同期数据

{chr(10).join(historical_lines)}

## 数据口径与校验

- 唯一数据源：东方财富5分钟指数K线JSON接口
- 沪市代码：`1.000001`；深市代码：`0.399001`
- 成交额字段：每根K线第7项`f57`，原始单位为元
- 使用K线：{target}目标对应 `{', '.join(EXPECTED_TIMES[target])}`
- 仅在沪深两侧目标K线均完整时生成成功报告
- 未使用新闻快讯、搜索摘要、实时快照、估算、插值或反推
"""
    payload = {
        "trade_date": trade_day.isoformat(),
        "target_time": target,
        "generated_at": generated_at.isoformat(),
        "shanghai_amount_yi": str(sh_yi),
        "shenzhen_amount_yi": str(sz_yi),
        "total_amount_yi": str(total_yi),
        "previous_day": previous_day_text,
        "previous_day_total_yi": (
            str(previous_total_yi) if previous_total_yi is not None else None
        ),
        "previous_day_change_amount_yi": (
            str(previous_diff_yi) if previous_diff_yi is not None else None
        ),
        "previous_day_change_pct": (
            str(previous_pct) if previous_pct is not None else None
        ),
        "five_day_average_yi": (
            str(five_avg_yi) if five_avg_yi is not None else None
        ),
        "five_day_change_amount_yi": (
            str(five_diff_yi) if five_diff_yi is not None else None
        ),
        "five_day_change_pct": str(five_pct) if five_pct is not None else None,
        "classification": label,
        "history_sample_count": len(previous_days),
        "status": "success",
        "source": "Eastmoney 5-minute index K-line JSON",
    }
    return report, payload


def save_success_report(
    trade_day: date,
    target: str,
    report: str,
    payload: dict,
) -> None:
    suffix = target.replace(":", "")
    dated_dir = REPORTS_DIR / trade_day.isoformat()
    write_text(dated_dir / f"{suffix}.md", report)
    write_json(dated_dir / f"{suffix}.json", payload)
    write_text(REPORTS_DIR / f"latest_{suffix}.md", report)
    write_json(REPORTS_DIR / f"latest_{suffix}.json", payload)
    update_history(payload)


def wait_until_target(target: str) -> None:
    now = now_cn()
    hour, minute = map(int, target.split(":"))
    target_dt = datetime.combine(now.date(), dt_time(hour, minute), TZ) + timedelta(seconds=45)
    if now < target_dt:
        seconds = (target_dt - now).total_seconds()
        # 安全上限：防止误操作导致作业等待过久
        if seconds > 20 * 60:
            raise RuntimeError(f"距离目标时点{target}超过20分钟，拒绝长时间等待")
        print(f"等待{seconds:.0f}秒，直到{target} K线封口后再取数……")
        time.sleep(seconds)


def collect(targets: Iterable[str], wait: bool) -> int:
    targets = tuple(targets)
    for target in targets:
        if target not in TARGETS:
            raise ValueError(f"不支持的目标时点：{target}")

    if wait and len(targets) == 1:
        wait_until_target(targets[0])

    trade_day = now_cn().date()
    last_error: Exception | None = None

    # 目标K线可能在时点后几十秒才进入接口；最多检查约3分钟。
    for round_no in range(1, 10):
        try:
            market_bars = load_market_bars()
            indexed = {
                market: index_bars_by_date(bars)
                for market, bars in market_bars.items()
            }

            generated = 0
            pending: list[str] = []
            for target in targets:
                values, missing = complete_total(indexed, trade_day, target)
                if values is None:
                    pending.append(target)
                    print(f"{trade_day} {target}目标K线尚不完整：{missing}")
                    continue

                previous = get_previous_complete_days(
                    indexed, trade_day, target, count=5
                )
                generated_at = now_cn()
                report, payload = build_report(
                    trade_day, target, values, previous, generated_at
                )
                save_success_report(trade_day, target, report, payload)
                print(
                    f"已生成{trade_day} {target}报告："
                    f"{payload['total_amount_yi']}亿元"
                )
                generated += 1

            if not pending:
                return 0
            if not wait:
                # 手动“全部回算”时，已过目标时点却仍缺失，应明确失败。
                now = now_cn()
                latest_target = max(targets)
                target_hour, target_minute = map(int, latest_target.split(":"))
                cutoff = datetime.combine(
                    trade_day, dt_time(target_hour, target_minute), TZ
                ) + timedelta(minutes=5)
                if now >= cutoff:
                    print(f"目标K线缺失，未生成：{', '.join(pending)}", file=sys.stderr)
                    return 2
                return 0

            if round_no < 9:
                print("等待20秒后重试目标K线……")
                time.sleep(20)
        except Exception as exc:
            last_error = exc
            print(f"第{round_no}轮取数失败：{exc}", file=sys.stderr)
            if round_no < 9:
                time.sleep(20)

    print(f"连续重试后仍未完成：{last_error}", file=sys.stderr)
    return 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        choices=("10:00", "10:15", "all"),
        default="all",
        help="生成指定时点；all会回算今天已经封口的两个时点",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="在定时任务中等待到目标时点封口，并对接口进行短暂重试",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    targets = TARGETS if args.target == "all" else (args.target,)
    return collect(targets, wait=args.wait)


if __name__ == "__main__":
    raise SystemExit(main())
