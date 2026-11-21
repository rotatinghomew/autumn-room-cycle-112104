# 客房周转工时的比较口径与跨楼协作

跨楼借班前核对交接放房及保留的资格。每条时间只与同日作业比较。

## 资料交换

`data/samples.json` 是合成样册。`fields` 给出有序列名，`rows` 按该顺序记录数据，`meaning` 标明单位或来源区别。没有填写的值用 `null` 表示。

- `rooms`：房型与实际需拆装儿童床独立。 列：`room`, `building`, `type`, `baby_bed`。
- `staff`：儿童床装配资格只在人员离岗前有效。 列：`person`, `team`, `skill`, `shift_start`, `shift_end`。
- `work`：rooms 可列共同作业的目标；evidence 区分确定和仅同时观测。 列：`ref`, `day`, `rooms`, `workers`, `start`, `end`, `evidence`。
- `interruptions`：环境暂停不是工时。 列：`ref`, `from_at`, `to_at`, `reason`, `known_seq`。
- `frontdesk`：released_by 为房间交给清洁的时间；deadline 是对客承诺。 列：`booking`, `room`, `released_by`, `deadline`, `state`。
- `shifts`：候选班次含人员覆盖窗；跨楼移动须耗时。 列：`option`, `person`, `building`, `start`, `end`。
- `handover`：登记的新交接时间。 列：`booking`, `released_by`, `seq`。
- `transit`：楼间最短步行时间。 列：`from_building`, `to_building`, `minutes`。
- `service_windows`：值班交接单上的儿童床作业窗；独立于普通清洁。 列：`booking`, `qualified_step`, `earliest`, `latest`, `min_minutes`。

## 检视

使用 Python 3.11，资料读取不依赖第三方包。

```sh
python3 source_book.py data/samples.json
python3 housekeeping.py data/samples.json            # 打印完整核对报告
python3 housekeeping.py data/samples.json --out report.json
python3 -m unittest discover -s tests -v
```

资料读取命令列出各表记录数；测试检查交叉引用与格式，不生成分析结果。

## 分析口径（基线与备选班次共用）

`housekeeping.py` 沿同一口径输出报告，重复运行结果一致：

1. **同日比较**：每条 `work` 只与同一 `day` 的记录比较钟点。
2. **中断剔除**：`interruptions` 与作业窗重叠的分钟从工时扣除，理由（住客延期、等儿童用品）单列，不计入效率。
3. **证据分级**：`确定` 记录进入单房基准与团队效率；`仅同时` 只给每房墙钟与人时区间（如净 15 分钟、两房 → 7.5–15 分钟/房），不推断共同作业覆盖范围。
4. **人时口径**：净墙钟 × 人数；多房共同作业按房均摊，并另算相对单人基准的墙钟节省与人时增量。共同作业是否省工时由增量回答，不把墙钟直接算给每间房。
5. **承诺范围**：只有 `已承诺` 的前台单进入承诺判断；`试算` 单只做可重排试算，不出现在前台承诺视图。
6. **可行性依据**：按个人覆盖窗核对，跨楼窗之间必须满足 `transit` 步行时间；儿童床步骤要求有资格人员在 `service_windows` 内做满 `min_minutes`。不使用班组总工时替代个人在岗时段。

报告各节：`工时台账`、`单房基准`/`房型基准`、`共同任务增量`、`仅同时观测区间`、
`团队可控工时`（外部暂停与不确定记录另列，不产出班组排名）、`有效放房时间`
（取 `seq` 最大的新交接并保留原时间）、`班次覆盖窗`、`资格覆盖`、
`班次可行性`（交接前/后两套）、`交接影响`、`前台承诺视图`。

## 交接变化与跨楼借调的读法

- `交接影响.newly_broken_by_handover`：新交接才破坏的承诺，附阻塞步骤。
- `must_release_or_rebook`：当前班次下必须解除或改签的承诺。
- `partial_coverage`：仍可调度但不能同口径承诺的步骤（如资格人员撤离后普通清洁可做、儿童床不可承诺）。
- `retained_commitments_other_building`：未受影响楼里仍可保留的承诺。
- `前台承诺视图` 不含人员姓名、班组与个人评价，只保留角色化原因；证据链（工单、人员、时段）保留在内部各节，可由承诺反查。

