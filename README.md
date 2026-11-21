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
python3 -m unittest -v
```

资料读取命令列出各表记录数；测试检查交叉引用与格式，不生成分析结果。
