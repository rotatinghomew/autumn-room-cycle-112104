"""客房周转工时分析与跨楼借调可行性核对。

口径（基线与备选班次共用，可重复比较）：

1. 每条 work 记录只与同一 day 的记录比较；跨日不比较钟点。
2. 中断区间与作业窗重叠的部分从墙钟时间中剔除，理由随记录保留，
   环境暂停不计入清洁人员工时，也不计入效率。
3. evidence 为“确定”的记录才进入基准与团队效率；“仅同时”只给
   每房时间与可比效率区间，不推断共同作业覆盖范围。
4. 多人作业的人时 = 净墙钟 × 人数；多房共同作业按房均摊，同时
   另算相对单房基准的墙钟节省与人时增量。
5. 前台承诺只覆盖 state 为“已承诺”的单；“试算”单只做可重排试算。
6. 班次可行性核对实际人员覆盖窗、楼间步行时间与儿童床资格窗，
   不使用班组总工时替代个人在岗时段。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_book import load

CLEAN = "清洁"
BABY = "儿童床"
CONFIRMED = "确定"
SIMULTANEOUS = "仅同时"
COMMITTED = "已承诺"
TRIAL = "试算"


# ---------------------------------------------------------------- 时间工具

def to_min(hhmm):
    """把 ``HH:MM`` 转为当日分钟数。"""
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def to_hhmm(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def overlap(start_a, end_a, start_b, end_b):
    """两个区间的重叠分钟数（端点相接不算重叠）。"""
    return max(0, min(end_a, end_b) - max(start_a, start_b))


# ---------------------------------------------------------------- 工时台账

def interruption_index(book):
    index = {}
    for row in book["interruptions"]:
        index.setdefault(row["ref"], []).append(row)
    for rows in index.values():
        rows.sort(key=lambda r: r["known_seq"])
    return index


def net_work(row, interruptions_for_ref):
    """返回 (总墙钟, 剔除分钟, 净墙钟, 剔除明细)。"""
    start, end = to_min(row["start"]), to_min(row["end"])
    gross = end - start
    excluded = []
    excluded_total = 0
    for item in interruptions_for_ref:
        cut = overlap(start, end, to_min(item["from_at"]), to_min(item["to_at"]))
        if cut:
            excluded_total += cut
            excluded.append({"reason": item["reason"], "minutes": cut, "seq": item["known_seq"]})
    return gross, excluded_total, gross - excluded_total, excluded


def build_work_ledger(book):
    """每条工单的净工时、人时与房均摊；证据不足时给区间。"""
    pauses = interruption_index(book)
    ledger = []
    for row in book["work"]:
        gross, excluded_total, net_minutes, excluded_detail = net_work(row, pauses.get(row["ref"], []))
        workers = row["workers"]
        rooms = row["rooms"]
        person_minutes = net_minutes * len(workers)
        entry = {
            "ref": row["ref"],
            "day": row["day"],
            "rooms": rooms,
            "workers": workers,
            "teams": sorted({_team_of(book, p) for p in workers}),
            "evidence": row["evidence"],
            "gross_minutes": gross,
            "excluded_minutes": excluded_total,
            "excluded_detail": excluded_detail,
            "net_minutes": net_minutes,
            "worker_count": len(workers),
            "person_minutes": person_minutes,
            "room_count": len(rooms),
        }
        if row["evidence"] == CONFIRMED:
            entry["per_room_minutes"] = round(person_minutes / len(rooms), 2)
            entry["per_room_wall_minutes"] = round(net_minutes / len(rooms), 2)
        else:
            # 只能确认同时在场：每房墙钟介于“完全并行”与“全程串行”之间。
            entry["per_room_wall_range"] = [
                round(net_minutes / len(rooms), 2),
                float(net_minutes),
            ]
            entry["per_room_person_range"] = [
                round(person_minutes / len(rooms), 2),
                float(person_minutes),
            ]
            entry["coverage_inference"] = "无法确定共同作业覆盖范围，仅给区间"
        ledger.append(entry)
    return ledger


def _team_of(book, person):
    for row in book["staff"]:
        if row["person"] == person:
            return row["team"]
    raise KeyError(person)


def _has_skill(book, person, skill):
    for row in book["staff"]:
        if row["person"] == person:
            return skill in row["skill"]
    return False


# ---------------------------------------------------------------- 单房基准

def solo_benchmarks(book, ledger):
    """确定证据的单人工单建立每房、每房型净墙钟基准。"""
    room_info = {r["room"]: r for r in book["rooms"]}
    by_room, by_type = {}, {}
    for entry in ledger:
        if entry["evidence"] != CONFIRMED or entry["worker_count"] != 1 or entry["room_count"] != 1:
            continue
        room = entry["rooms"][0]
        by_room.setdefault(room, []).append(entry["net_minutes"])
        rtype = room_info[room]["type"]
        by_type.setdefault(rtype, []).append(entry["net_minutes"])

    def summarize(bucket):
        return {
            key: {"mean_minutes": round(sum(vals) / len(vals), 2), "n": len(vals), "values": vals}
            for key, vals in sorted(bucket.items())
        }

    room_bench = summarize(by_room)
    type_bench = summarize(by_type)
    for room in room_info:
        if room not in room_bench:
            room_bench[room] = {"mean_minutes": None, "n": 0, "values": [], "gap": "无确定单人工单，无法建立基准"}
    for rtype in sorted({r["type"] for r in book["rooms"]}):
        if rtype not in type_bench:
            type_bench[rtype] = {"mean_minutes": None, "n": 0, "values": [], "gap": "无确定单人工单，无法建立基准"}
    return room_bench, type_bench


# ---------------------------------------------------------------- 共同任务增量

def shared_task_increments(ledger, room_bench):
    """确定共同任务相对单房基准的墙钟节省与人时增量。"""
    findings = []
    for entry in ledger:
        if entry["evidence"] != CONFIRMED or entry["worker_count"] < 2:
            continue
        solo_wall, missing = 0, []
        for room in entry["rooms"]:
            mean = room_bench.get(room, {}).get("mean_minutes")
            if mean is None:
                missing.append(room)
            else:
                solo_wall += mean
        item = {
            "ref": entry["ref"],
            "rooms": entry["rooms"],
            "shared_wall_minutes": entry["net_minutes"],
            "shared_person_minutes": entry["person_minutes"],
            "solo_benchmark_total": None if missing else round(solo_wall, 2),
            "wall_saving_minutes": None if missing else round(solo_wall - entry["net_minutes"], 2),
            "person_minute_delta": None if missing else round(entry["person_minutes"] - solo_wall, 2),
        }
        if missing:
            item["gap"] = f"房间 {','.join(missing)} 缺单房基准，增量不可估"
        findings.append(item)
    return findings


def simultaneous_ranges(ledger):
    """仅同时观测：给时间与可比效率区间，不推断共同范围。"""
    out = []
    for entry in ledger:
        if entry["evidence"] != SIMULTANEOUS:
            continue
        out.append({
            "ref": entry["ref"],
            "day": entry["day"],
            "rooms": entry["rooms"],
            "net_minutes": entry["net_minutes"],
            "per_room_wall_range": entry["per_room_wall_range"],
            "per_room_person_range": entry["per_room_person_range"],
            "note": entry["coverage_inference"],
        })
    return out


# ---------------------------------------------------------------- 团队可控工时

def team_efficiency(book, ledger):
    """区分团队可控效率与房型、任务组合、外部条件差异。"""
    teams = {}
    for entry in ledger:
        for team in entry["teams"]:
            bucket = teams.setdefault(team, {
                "confirmed_person_minutes": 0,
                "confirmed_room_tasks": 0,
                "shared_refs": [],
                "solo_refs": [],
                "excluded_external_minutes": 0,
                "excluded_reasons": {},
                "uncertain_refs": [],
                "room_mix": {},
            })
            for detail in entry["excluded_detail"]:
                bucket["excluded_external_minutes"] += detail["minutes"]
                bucket["excluded_reasons"][detail["reason"]] = (
                    bucket["excluded_reasons"].get(detail["reason"], 0) + detail["minutes"]
                )
            if entry["evidence"] != CONFIRMED:
                bucket["uncertain_refs"].append(entry["ref"])
                continue
            bucket["confirmed_person_minutes"] += entry["person_minutes"]
            bucket["confirmed_room_tasks"] += entry["room_count"]
            for room in entry["rooms"]:
                rtype = next(r["type"] for r in book["rooms"] if r["room"] == room)
                key = f"{room}/{rtype}"
                bucket["room_mix"][key] = bucket["room_mix"].get(key, 0) + 1
            (bucket["shared_refs"] if entry["worker_count"] > 1 else bucket["solo_refs"]).append(entry["ref"])

    for bucket in teams.values():
        bucket["person_minutes_per_room_task"] = (
            round(bucket["confirmed_person_minutes"] / bucket["confirmed_room_tasks"], 2)
            if bucket["confirmed_room_tasks"] else None
        )
    return teams


def like_for_like_solo(book, ledger):
    """同房型单人工单的跨团队对照；样本量随结果给出，不做排名。"""
    rows = []
    for entry in ledger:
        if entry["evidence"] == CONFIRMED and entry["worker_count"] == 1 and entry["room_count"] == 1:
            room = entry["rooms"][0]
            rtype = next(r["type"] for r in book["rooms"] if r["room"] == room)
            rows.append({
                "ref": entry["ref"],
                "day": entry["day"],
                "room": room,
                "type": rtype,
                "team": entry["teams"][0],
                "net_minutes": entry["net_minutes"],
            })
    paired = {}
    for row in rows:
        paired.setdefault(row["type"], []).append(row)
    return {rtype: items for rtype, items in paired.items() if len({i["team"] for i in items}) > 1}


# ---------------------------------------------------------------- 交接与班次

def effective_releases(book):
    """每笔前台单的有效放房时间：取 seq 最大的新交接，否则用原记录。"""
    latest = {}
    for row in book["handover"]:
        if row["booking"] not in latest or row["seq"] > latest[row["booking"]]["seq"]:
            latest[row["booking"]] = row
    result = {}
    for fd in book["frontdesk"]:
        booking = fd["booking"]
        if booking in latest:
            result[booking] = {
                "released_by": to_min(latest[booking]["released_by"]),
                "released_by_hhmm": latest[booking]["released_by"],
                "source": "handover",
                "seq": latest[booking]["seq"],
                "original": to_min(fd["released_by"]),
            }
        else:
            result[booking] = {
                "released_by": to_min(fd["released_by"]),
                "released_by_hhmm": fd["released_by"],
                "source": "frontdesk",
                "seq": None,
                "original": to_min(fd["released_by"]),
            }
    return result


def option_windows(book):
    """候选班次 → 人员 → 覆盖窗（含楼间步行校验），并按人员离岗时间裁剪。

    候选班次只列出受调整人员；未列出的人员按 staff 在岗时段保留可用性，
    其所在楼取当天 work 记录出现过的楼。
    """
    shifts = {}
    staff_end = {s["person"]: (to_min(s["shift_start"]), to_min(s["shift_end"])) for s in book["staff"]}
    for row in book["shifts"]:
        shifts.setdefault(row["option"], {}).setdefault(row["person"], []).append({
            "building": row["building"],
            "start": to_min(row["start"]),
            "end": to_min(row["end"]),
        })

    default_buildings = {}
    for row in book["work"]:
        building = next(r["building"] for r in book["rooms"] if r["room"] == row["rooms"][0])
        for person in row["workers"]:
            default_buildings.setdefault(person, set()).add(building)

    transit = {(t["from_building"], t["to_building"]): t["minutes"] for t in book["transit"]}
    report = {}
    for option, persons in shifts.items():
        merged = {person: [dict(w, source="explicit") for w in windows] for person, windows in persons.items()}
        for person, (shift_start, shift_end) in staff_end.items():
            if person in merged:
                continue
            for building in sorted(default_buildings.get(person, ())):
                merged.setdefault(person, []).append({
                    "building": building,
                    "start": shift_start,
                    "end": shift_end,
                    "source": "default",
                })
        per_option, violations = {}, []
        for person, windows in merged.items():
            windows = sorted(windows, key=lambda w: w["start"])
            shift_start, shift_end = staff_end[person]
            clipped = []
            for w in windows:
                if w["start"] < shift_start or w["end"] > shift_end:
                    violations.append({"person": person, "window": w, "reason": "超出人员在岗时段"})
                clipped.append({**w, "start": max(w["start"], shift_start), "end": min(w["end"], shift_end)})
            # 步行校验只针对候选班次显式安排的相邻窗；默认窗是“在该楼可用”的独立假设。
            explicit = [w for w in clipped if w["source"] == "explicit"]
            for prev, nxt in zip(explicit, explicit[1:]):
                if prev["building"] != nxt["building"]:
                    gap = nxt["start"] - prev["end"]
                    need = transit.get((prev["building"], nxt["building"]))
                    if need is None or gap < need:
                        violations.append({
                            "person": person,
                            "from": prev,
                            "to": nxt,
                            "gap_minutes": gap,
                            "required_minutes": need,
                            "reason": "楼间移动时间不足",
                        })
            per_option[person] = clipped
        report[option] = {"windows": per_option, "violations": violations}
    return report


def qualification_coverage(book, windows_report):
    """每个候选班次下，各楼是否仍有儿童床资格覆盖。"""
    qualified = {s["person"] for s in book["staff"] if BABY in s["skill"]}
    out = {}
    for option, payload in windows_report.items():
        coverage = {}
        for person, windows in payload["windows"].items():
            if person not in qualified:
                continue
            for w in windows:
                coverage.setdefault(w["building"], []).append(
                    {"person": person, "start": to_hhmm(w["start"]), "end": to_hhmm(w["end"])}
                )
        for building in {r["building"] for r in book["rooms"]}:
            coverage.setdefault(building, [])
        out[option] = coverage
    return out


def _earliest_slot(windows, length, earliest, latest, busy=()):
    """在覆盖窗内找最早的不冲突空闲时段，返回开始分钟或 None。"""
    for w in sorted(windows, key=lambda x: x["start"]):
        cursor = max(earliest, w["start"])
        while cursor + length <= min(w["end"], latest):
            slot = (cursor, cursor + length)
            if all(overlap(slot[0], slot[1], b[0], b[1]) == 0 for b in busy):
                return cursor
            cursor += 1
    return None


def evaluate_booking(book, windows_payload, release_min, fd, room_bench):
    """对单笔前台单核对普通清洁与儿童床资格窗。

    儿童床与普通清洁是两个独立步骤，调度时枚举两种工序顺序，
    并允许由不同人员承担；取最早完成的可行排法。
    """
    room_info = next(r for r in book["rooms"] if r["room"] == fd["room"])
    building, room = room_info["building"], room_info["room"]
    deadline = to_min(fd["deadline"])
    windows_by_person = windows_payload["windows"]
    in_building = {
        person: [w for w in windows if w["building"] == building]
        for person, windows in windows_by_person.items()
    }

    # 任务规格：清洁须在放房后、承诺前完成；儿童床另受资格窗约束。
    window_row = next((w for w in book["service_windows"] if w["booking"] == fd["booking"]), None)
    duration = room_bench.get(room, {}).get("mean_minutes")
    tasks = []
    if window_row is not None:
        tasks.append({
            "step": BABY,
            "required_minutes": window_row["min_minutes"],
            "earliest": max(release_min, to_min(window_row["earliest"])),
            "latest": to_min(window_row["latest"]),
            "window_hhmm": [to_hhmm(max(release_min, to_min(window_row["earliest"]))), window_row["latest"]],
            "needs_qualification": True,
        })
    if duration is None:
        clean_status = ("evidence_gap", "该房无确定单人清洁基准，不能按同一口径承诺")
    else:
        tasks.append({
            "step": CLEAN,
            "required_minutes": int(duration) if float(duration).is_integer() else duration,
            "earliest": release_min,
            "latest": deadline,
            "window_hhmm": [to_hhmm(release_min), to_hhmm(deadline)],
            "needs_qualification": False,
        })
        clean_status = None

    def eligible(person, spec):
        skill = BABY if spec["needs_qualification"] else CLEAN
        return _has_skill(book, person, skill)

    def schedule_one(spec, busy):
        """单个任务在给定占用表上找最早可行时段。"""
        choice = None
        for person in sorted(windows_by_person):
            if not eligible(person, spec):
                continue
            start = _earliest_slot(
                in_building[person],
                spec["required_minutes"],
                spec["earliest"],
                spec["latest"],
                busy.get(person, []),
            )
            if start is not None and (choice is None or start < choice["start"]):
                choice = {"person": person, "start": start, "end": start + spec["required_minutes"]}
        return choice

    def try_order(ordered):
        """按给定工序顺序贪心安排，允许不同人分担；逐任务独立判定。"""
        busy = {}
        plan = {}
        for spec in ordered:
            choice = schedule_one(spec, busy)
            if choice is not None:
                busy.setdefault(choice["person"], []).append((choice["start"], choice["end"]))
                plan[spec["step"]] = choice
        return plan

    plans = [try_order(tasks), try_order(list(reversed(tasks)))] if tasks else []
    # 每个任务取各顺序下最早的可行安排；某任务在所有顺序都无解才判不可行。
    plan_by_step = {}
    for spec in tasks:
        choices = [plan[spec["step"]] for plan in plans if spec["step"] in plan]
        if choices:
            plan_by_step[spec["step"]] = min(choices, key=lambda c: c["start"])

    result = {
        "booking": fd["booking"],
        "room": room,
        "building": building,
        "scope": fd["state"],
        "deadline": to_hhmm(deadline),
        "tasks": [],
    }
    for spec in tasks:
        item = {
            "step": spec["step"],
            "required_minutes": spec["required_minutes"],
            "window": spec["window_hhmm"],
        }
        choice = plan_by_step.get(spec["step"])
        if choice is None:
            if spec["step"] == BABY:
                item.update(status="infeasible", reason="资格窗内无具备儿童床资格的人员可做满作业分钟")
            else:
                item.update(status="infeasible", reason="放房后至承诺时刻前无人可在该楼完成基准清洁")
        else:
            item.update(
                status="feasible",
                assignee=choice["person"],
                slot=[to_hhmm(choice["start"]), to_hhmm(choice["end"])],
                slack_minutes=spec["latest"] - choice["end"],
            )
        result["tasks"].append(item)
    if clean_status is not None:
        result["tasks"].append({
            "step": CLEAN,
            "status": clean_status[0],
            "reason": clean_status[1],
        })

    statuses = {t["status"] for t in result["tasks"]}
    if statuses == {"feasible"}:
        result["status"] = "feasible"
    elif "infeasible" in statuses:
        result["status"] = "infeasible"
    else:
        result["status"] = "evidence_gap"
    return result


def shift_feasibility(book, room_bench):
    """基线与备选班次沿同一口径逐单核对（含交接前/后两套放房时间）。"""
    windows_report = option_windows(book)
    releases = effective_releases(book)
    options = {}
    for option, payload in windows_report.items():
        per_option = {"window_violations": payload["violations"], "before_handover": [], "after_handover": []}
        for fd in book["frontdesk"]:
            rel = releases[fd["booking"]]
            after = evaluate_booking(book, payload, rel["released_by"], fd, room_bench)
            before = evaluate_booking(book, payload, rel["original"], fd, room_bench)
            per_option["after_handover"].append(after)
            per_option["before_handover"].append(before)
        options[option] = per_option
    return options, windows_report, releases


# ---------------------------------------------------------------- 交接影响

def handover_impact(feasibility):
    """新交接导致的变化：可重排部分、必须解除/改签的承诺、另一楼保留项。"""
    summary = {}
    for option, payload in feasibility.items():
        before = {r["booking"]: r for r in payload["before_handover"]}
        after = {r["booking"]: r for r in payload["after_handover"]}
        newly_broken, still_ok, rebook, reschedulable = [], [], [], []
        for booking, after_row in after.items():
            before_row = before[booking]
            if after_row["scope"] != COMMITTED:
                reschedulable.append({
                    "booking": booking,
                    "building": after_row["building"],
                    "status": after_row["status"],
                    "note": "试算单，不构成对客承诺，可随班次重排",
                })
                continue
            if before_row["status"] == "feasible" and after_row["status"] != "feasible":
                newly_broken.append({
                    "booking": booking,
                    "building": after_row["building"],
                    "before": before_row["status"],
                    "after": after_row["status"],
                    "blocking_tasks": [t["step"] for t in after_row["tasks"] if t["status"] != "feasible"],
                })
                rebook.append(booking)
            elif after_row["status"] == "feasible":
                still_ok.append(booking)
            else:
                rebook.append(booking)
        broken_buildings = {b["building"] for b in newly_broken}
        retained = []
        for booking, after_row in after.items():
            if (
                after_row["scope"] == COMMITTED
                and after_row["status"] == "feasible"
                and after_row["building"] not in broken_buildings
            ):
                retained.append({"booking": booking, "building": after_row["building"], "status": after_row["status"]})
        # 仍可调度但不能同口径承诺的部分（如资格撤离后的普通清洁）。
        partial = []
        for booking, after_row in after.items():
            bad = [t for t in after_row["tasks"] if t["status"] != "feasible"]
            good = [t for t in after_row["tasks"] if t["status"] == "feasible"]
            if bad and good:
                partial.append({
                    "booking": booking,
                    "still_schedulable": [t["step"] for t in good],
                    "cannot_promise": [t["step"] for t in bad],
                })
        summary[option] = {
            "newly_broken_by_handover": newly_broken,
            "must_release_or_rebook": sorted(set(rebook)),
            "committed_feasible": still_ok,
            "reschedulable_trials": reschedulable,
            "partial_coverage": partial,
            "retained_commitments_other_building": retained,
        }
    return summary


# ---------------------------------------------------------------- 前台视图

def frontdesk_view(feasibility):
    """对客/前台层：不含人员姓名、班组与个人评价，只留角色化原因与承诺状态。"""
    views = {}
    role_reason = {
        "资格窗内无具备儿童床资格的人员可做满作业分钟": "儿童床作业窗内资格人员时段不足",
        "放房后至承诺时刻前无人可在该楼完成基准清洁": "放房后可用清洁时段不足",
        "该房无确定单人清洁基准，不能按同一口径承诺": "该房型工时基准不足，暂不按同口径承诺",
    }
    for option, payload in feasibility.items():
        rows = []
        for row in payload["after_handover"]:
            if row["scope"] != COMMITTED:
                continue
            rows.append({
                "booking": row["booking"],
                "building": row["building"],
                "room": row["room"],
                "deadline": row["deadline"],
                "promise": {
                    "feasible": "可承诺",
                    "infeasible": "需解除或改签",
                    "evidence_gap": "基准不足暂缓承诺",
                }[row["status"]],
                "reasons": [role_reason.get(t.get("reason"), t["step"]) for t in row["tasks"] if t["status"] != "feasible"],
            })
        views[option] = rows
    return views


# ---------------------------------------------------------------- 汇总报告

def build_report(path):
    book = load(path)
    ledger = build_work_ledger(book)
    room_bench, type_bench = solo_benchmarks(book, ledger)
    feasibility, windows_report, releases = shift_feasibility(book, room_bench)
    return {
        "口径": {
            "同日比较": "work 记录仅与同一 day 的记录比较钟点",
            "中断剔除": "与作业窗重叠的暂停分钟不计工时，理由单列",
            "证据分级": "确定进入基准与效率；仅同时只给区间，不推断共同覆盖",
            "人时口径": "净墙钟 × 人数，共同作业另算墙钟节省与人时增量",
            "承诺范围": "仅已承诺前台单进入承诺判断，试算单只做可重排试算",
            "承诺依据": "按个人覆盖窗、楼间步行与资格窗核对，不用班组总工时替代",
        },
        "工时台账": ledger,
        "单房基准": room_bench,
        "房型基准": type_bench,
        "共同任务增量": shared_task_increments(ledger, room_bench),
        "仅同时观测区间": simultaneous_ranges(ledger),
        "团队可控工时": team_efficiency(book, ledger),
        "同房型单人对照": like_for_like_solo(book, ledger),
        "有效放房时间": {
            booking: {k: (to_hhmm(v) if k in ("released_by", "original") and isinstance(v, int) else v)
                      for k, v in info.items()}
            for booking, info in releases.items()
        },
        "班次覆盖窗": {
            option: {
                "violations": payload["violations"],
                "persons": {
                    person: [{"building": w["building"], "window": [to_hhmm(w["start"]), to_hhmm(w["end"])],
                              "source": w["source"]}
                              for w in windows]
                    for person, windows in payload["windows"].items()
                },
            }
            for option, payload in windows_report.items()
        },
        "资格覆盖": qualification_coverage(book, windows_report),
        "班次可行性": feasibility,
        "交接影响": handover_impact(feasibility),
        "前台承诺视图": frontdesk_view(feasibility),
    }


def main():
    parser = argparse.ArgumentParser(description="按统一口径生成客房周转工时与跨楼协作核对报告")
    parser.add_argument("path", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="报告输出路径，缺省打印到标准输出")
    args = parser.parse_args()
    text = json.dumps(build_report(args.path), ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
