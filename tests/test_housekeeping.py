"""分析口径与可行性核对的测试。"""
from pathlib import Path
import unittest

from housekeeping import (
    build_report,
    build_work_ledger,
    effective_releases,
    handover_impact,
    interruption_index,
    net_work,
    option_windows,
    overlap,
    shared_task_increments,
    simultaneous_ranges,
    solo_benchmarks,
    team_efficiency,
    to_min,
)
from source_book import load

DATA = Path(__file__).resolve().parents[1] / "data" / "samples.json"


class TimeToolTest(unittest.TestCase):
    def test_to_min(self):
        self.assertEqual(to_min("00:00"), 0)
        self.assertEqual(to_min("11:40"), 700)

    def test_overlap(self):
        self.assertEqual(overlap(0, 10, 5, 15), 5)
        self.assertEqual(overlap(0, 10, 10, 20), 0)
        self.assertEqual(overlap(0, 10, 20, 30), 0)


class LedgerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = load(DATA)
        cls.ledger = build_work_ledger(cls.book)
        cls.by_ref = {e["ref"]: e for e in cls.ledger}

    def test_interruption_excluded_from_work_minutes(self):
        # w2: 16 分钟墙钟，中间 6 分钟等儿童用品，净工时 10。
        w2 = self.by_ref["w2"]
        self.assertEqual((w2["gross_minutes"], w2["excluded_minutes"], w2["net_minutes"]), (16, 6, 10))
        self.assertEqual(w2["excluded_detail"][0]["reason"], "等儿童用品")
        # w6: 29 分钟墙钟，13 分钟住客延期。
        w6 = self.by_ref["w6"]
        self.assertEqual((w6["gross_minutes"], w6["excluded_minutes"], w6["net_minutes"]), (29, 13, 16))

    def test_partial_overlap_interruption_is_clipped_to_window(self):
        # w3 暂停 11:07-11:11 与作业窗 11:07-11:26 重叠 4 分钟。
        row = next(r for r in self.book["work"] if r["ref"] == "w3")
        gross, excluded, net, _ = net_work(row, interruption_index(self.book)["w3"])
        self.assertEqual((gross, excluded, net), (19, 4, 15))

    def test_person_minutes_count_every_worker(self):
        # 两人共同 19 分钟，人时按两人计，不能只算一个人。
        w1 = self.by_ref["w1"]
        self.assertEqual(w1["worker_count"], 2)
        self.assertEqual(w1["person_minutes"], 38)
        self.assertEqual(w1["per_room_minutes"], 19)

    def test_simultaneous_evidence_only_gets_ranges(self):
        ranges = {e["ref"]: e for e in simultaneous_ranges(self.ledger)}
        self.assertEqual(set(ranges), {"w3"})
        # 净 15 分钟、两房：每房墙钟介于 7.5 与 15 之间。
        self.assertEqual(ranges["w3"]["per_room_wall_range"], [7.5, 15.0])
        self.assertIn("无法确定", ranges["w3"]["note"])
        # 仅同时记录不进入确定基准。
        room_bench, _ = solo_benchmarks(self.book, self.ledger)
        self.assertNotIn("w3", str(room_bench))
        self.assertIsNone(room_bench["B1"]["mean_minutes"])


class BenchmarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = load(DATA)
        cls.ledger = build_work_ledger(cls.book)
        cls.room_bench, cls.type_bench = solo_benchmarks(cls.book, cls.ledger)

    def test_solo_benchmarks_use_net_minutes(self):
        self.assertEqual(self.room_bench["A1"]["mean_minutes"], 10.0)
        self.assertEqual(self.room_bench["A2"]["mean_minutes"], 22.0)
        self.assertEqual(self.room_bench["B2"]["mean_minutes"], 25.0)

    def test_shared_increment_against_solo_baseline(self):
        findings = {f["ref"]: f for f in shared_task_increments(self.ledger, self.room_bench)}
        # w1 两房单人基准合计 32，共同墙钟 19：省 13 分钟墙钟，人时多 6。
        self.assertEqual(findings["w1"]["wall_saving_minutes"], 13.0)
        self.assertEqual(findings["w1"]["person_minute_delta"], 6.0)
        # w6 单人房两人做：墙钟反而多 6 分钟，人时多 22。
        self.assertEqual(findings["w6"]["wall_saving_minutes"], -6.0)
        self.assertEqual(findings["w6"]["person_minute_delta"], 22.0)


class TeamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = load(DATA)
        cls.ledger = build_work_ledger(cls.book)
        cls.teams = team_efficiency(cls.book, cls.ledger)

    def test_external_pauses_kept_outside_efficiency(self):
        east = self.teams["东队"]
        self.assertEqual(east["confirmed_person_minutes"], 100)
        self.assertEqual(east["excluded_external_minutes"], 19)
        self.assertEqual(east["excluded_reasons"], {"等儿童用品": 6, "住客延期": 13})

    def test_uncertain_refs_excluded_from_team_efficiency(self):
        west = self.teams["西队"]
        self.assertNotIn("w3", west["solo_refs"] + west["shared_refs"])
        self.assertEqual(west["uncertain_refs"], ["w3"])
        self.assertEqual(west["person_minutes_per_room_task"], 23.5)

    def test_metric_is_not_a_ranking(self):
        # 口径结果只给数值与样本量，不产生班组速度排名字段。
        for bucket in self.teams.values():
            self.assertNotIn("rank", bucket)
            self.assertGreaterEqual(bucket["confirmed_room_tasks"], 1)


class ShiftTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = load(DATA)
        ledger = build_work_ledger(cls.book)
        cls.room_bench, _ = solo_benchmarks(cls.book, ledger)

    def test_latest_handover_wins_and_original_kept(self):
        releases = effective_releases(self.book)
        self.assertEqual(releases["f1"]["released_by"], to_min("12:27"))
        self.assertEqual(releases["f1"]["original"], to_min("11:40"))
        self.assertEqual(releases["f3"]["source"], "frontdesk")

    def test_transit_gap_enforced_between_explicit_windows(self):
        # 原班 P1 甲楼 13:20 结束、乙楼 13:30 开始，间隔正好 10 分钟，合规。
        report = option_windows(self.book)
        self.assertEqual(report["原班"]["violations"], [])
        # 缩短楼间步行时间到 15 分钟应触发违规。
        book = {**self.book, "transit": [
            {"from_building": "甲楼", "to_building": "乙楼", "minutes": 15},
            {"from_building": "乙楼", "to_building": "甲楼", "minutes": 15},
        ]}
        self.assertTrue(option_windows(book)["原班"]["violations"])

    def test_unlisted_staff_keep_default_availability_without_false_violation(self):
        report = option_windows(self.book)
        # P2 在借调中有显式甲楼窗；P3 的默认双楼窗不应报步行违规。
        self.assertEqual(report["借调"]["violations"], [])
        p2_buildings = {w["building"] for w in report["借调"]["windows"]["P2"]}
        self.assertEqual(p2_buildings, {"甲楼"})


class FeasibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = build_report(DATA)
        cls.feas = cls.report["班次可行性"]

    def _row(self, option, phase, booking):
        return next(r for r in self.feas[option][phase] if r["booking"] == booking)

    def test_baseline_promises_after_handover(self):
        f1 = self._row("原班", "after_handover", "f1")
        f4 = self._row("原班", "after_handover", "f4")
        f2 = self._row("原班", "after_handover", "f2")
        self.assertEqual(f1["status"], "feasible")
        self.assertEqual(f4["status"], "feasible")
        # f2 在原班就缺乙楼资格覆盖，且 B1 无清洁基准。
        self.assertEqual(f2["status"], "infeasible")
        self.assertIn("evidence_gap", {t["status"] for t in f2["tasks"]})

    def test_loan_option_loses_baby_bed_qualification_but_keeps_normal_room(self):
        f1 = self._row("借调", "after_handover", "f1")
        self.assertEqual(f1["status"], "infeasible")
        by_step = {t["step"]: t for t in f1["tasks"]}
        self.assertEqual(by_step["儿童床"]["status"], "infeasible")
        # 普通房仍可由留在甲楼的 P2 调度。
        self.assertEqual(by_step["清洁"]["status"], "feasible")
        self.assertEqual(by_step["清洁"]["assignee"], "P2")

    def test_collaboration_breaks_only_after_new_handover(self):
        before = self._row("协作", "before_handover", "f1")
        after = self._row("协作", "after_handover", "f1")
        self.assertEqual(before["status"], "feasible")
        self.assertEqual(after["status"], "infeasible")
        impact = handover_impact(self.feas)["协作"]
        self.assertEqual([b["booking"] for b in impact["newly_broken_by_handover"]], ["f1"])
        # 另一栋楼的承诺保留。
        retained = {r["booking"] for r in impact["retained_commitments_other_building"]}
        self.assertIn("f4", retained)
        self.assertNotIn("f1", retained)

    def test_trial_booking_never_treated_as_guest_promise(self):
        for option in self.feas:
            for phase in ("before_handover", "after_handover"):
                f3 = self._row(option, phase, "f3")
                self.assertEqual(f3["scope"], "试算")
        view = self.report["前台承诺视图"]
        for rows in view.values():
            self.assertNotIn("f3", {r["booking"] for r in rows})

    def test_frontdesk_view_has_no_personal_identifiers(self):
        text = str(self.report["前台承诺视图"])
        for person in ("P1", "P2", "P3", "P4", "东队", "西队"):
            self.assertNotIn(person, text)


if __name__ == "__main__":
    unittest.main()
