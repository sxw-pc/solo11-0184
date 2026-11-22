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

独立入口：voyage-import、vehicle-import、stow-import。每项导入格式为 `--db state.sqlite --file fixtures/<name>.json`。同源重复幂等，异内容复用事件 ID 则整批回滚。`audit` 在空库重放原始文件并比对业务状态。
