# 滚装渡轮车辆配载采证台

航次、车辆验车与配载三账分别决定潮窗运力、登船资格及离港放行。

Python 3.11+ / SQLite，合成数据离线运行。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m ferryload demo --db demo.sqlite
python -m ferryload report --db demo.sqlite
python -m ferryload audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：voyage-import、vehicle-import、stow-import、scale-import；多段采录包用 bundle-import（`{"voyage":[...],"vehicle":[...],"stow":[...],"scale":[...]}`，每段独立溯源）。每项导入格式为 `--db state.sqlite --file fixtures/<name>.json`。同源重复幂等，异内容复用事件 ID 则整批回滚。`audit` 在空库重放原始文件并比对业务状态。

磅票与校准记录（kind=scale）按（秤号， 校准周期）分组共享同一未知零点偏差：票重=读数-组偏差，不同秤/周期不合并；票号去重、复测/撤销按 revision 取代，换票可 supersedes 旧票。每个涉及磅票的航次签发可复算的重量与稳性边界证书（偏差区间、全船重量上下界、重心极值及达到边界的证据票），随 revision 留存于 certificates 表，审计重放保留实际使用的校准版本。样例见 fixtures/scale-shared-drift.json。
