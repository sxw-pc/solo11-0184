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

独立入口：voyage-import、vehicle-import、stow-import、gauge-import、channel-import、restow-import、sign-import。每项导入格式为 `--db state.sqlite --file fixtures/<name>.json`。同源重复幂等，异内容复用事件 ID 则整批回滚。`audit` 在空库重放原始文件并比对业务状态。

## 浅水通道连续包络裁定

`tide.evaluate` 保留离散潮窗的布尔裁定（离港时刻是否落在足够潮位的窗口、窗口是否冲突）。新增 `tide.assess_channel` 走连续物理路径：带时间戳的两端（及中途）水尺原件、浅滩沿程包络（分段上下界 + 最大涨落速率）、航速区间与按实载重量计算的船体吃水，共同给出全程净水深的可行上下界、最危险时刻和最坏/最好曲线见证。

安全结论必须对**所有与原件一致的连续曲线**成立：水尺 pin 通过速率锥投射成连续约束，与包络带取交集，而不是在离散采样点之间做线性插值。只给两端 220 cm 时，11:00 的可行带是 170–220 cm，存在一条合法但在浅滩差 10 cm 的曲线，因此裁定“冻结待补测”；端点线性插值或加密离散点都无法排除未采样谷值。

两条合法恢复链（见 `fixtures/tide-valley.json` 与 `tests/test_tide_valley.py`）：

- **补测**：中途增派独立校准水尺并验证无未测低谷，两车原班运输；
- **卸车**：卸下较重车辆并在已验证稳性的中心线舱位重排，吃水随实载重量下降，原班只运余车、重车进入下一班候补，且新装载版本须重新取得船长签认。

`departure_certificate` 表 append-only 保存每个装载版本的输入指纹、运车清单与曲线见证；晚到水尺按当时班次与装载版本回放，只追加新修订，历史放行/冻结证书不被覆盖。

