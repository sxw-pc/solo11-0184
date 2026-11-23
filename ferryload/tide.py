"""潮位与浅水通道工程裁定。

本模块保留两条互相独立的裁定路径：

* :func:`evaluate` —— 既有逻辑，只把 ``departure_at`` 与离散 ``tide_windows``
  做布尔比较，并检查窗口之间是否冲突。它不读取任何外部水尺或连续包络。
* :func:`assess_channel` —— 新的连续物理路径。它把两端（及中途）带时间戳的
  水尺原件、浅滩沿程包络、航速区间和按实载重量计算的吃水合成“全程净水深”
  裁定。安全结论必须对所有与原件一致的连续曲线成立，而不是在离散采样点之间
  做线性插值后选一条好看的曲线。
"""

from datetime import datetime, timedelta


def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def evaluate(v):
    """既有潮窗布尔裁定：离港时刻落在足够潮位的窗口且窗口之间不冲突。"""
    departure = _instant(v['departure_at'])
    windows = v.get('tide_windows', [])
    current = [w for w in windows if _instant(w['start']) <= departure < _instant(w['end']) and w['level_cm'] >= v['required_level_cm']]
    conflict = any(_instant(a['start']) < _instant(b['end']) and _instant(b['start']) < _instant(a['end']) and a['level_cm'] != b['level_cm'] for i, a in enumerate(windows) for b in windows[i + 1:])
    return bool(current) and not conflict, conflict


# ---------------------------------------------------------------------------
# 吃水：按实载重量计算，而不是只改 required_level_cm
# ---------------------------------------------------------------------------

def hull_draft_cm(voyage, boarded_weight_kg):
    """由空船吃水与载重响应推算实载吃水，卸下/加装车辆立即改变吃水。

    支持两种船体参数（``voyage['hull']``）：

    * ``cm_per_kg`` —— 每公斤实载重量产生的下沉厘米数（小载重变化范围的
      线性近似），与 ``light_draft_cm`` 相加；
    * ``tonnes_per_cm`` (TPC) —— 每厘米吃水吨量，按吨量除算下沉量。

    两者都缺省时吃水等于空船吃水，使没有船体参数的旧航次仍可运行。
    """
    hull = voyage.get('hull', {})
    light = float(hull.get('light_draft_cm', 0))
    if hull.get('cm_per_kg') is not None:
        return light + float(hull['cm_per_kg']) * float(boarded_weight_kg)
    tpc = float(hull.get('tonnes_per_cm', 0))
    if tpc <= 0:
        return light
    return light + (boarded_weight_kg / 1000.0) / tpc


def required_level_for(draft_cm, net_clearance_cm):
    """满足给定净水深所需的最低潮位（厘米）。"""
    return draft_cm + net_clearance_cm


# ---------------------------------------------------------------------------
# 水尺原件校验
# ---------------------------------------------------------------------------

def valid_gauges(gauges):
    """仅保留校准有效、观测时刻落在校准窗口内的水尺原件。"""
    out = []
    for g in gauges:
        if not g.get('calibrated', False):
            continue
        try:
            observed = _instant(g['observed_at'])
            start = _instant(g['calibration_start'])
            end = _instant(g['calibration_end'])
        except (KeyError, ValueError):
            continue
        if start <= observed < end:
            out.append(g)
    return out


def _interp(points, t):
    """对 ``[(time, value), ...]`` 按时间线性插值（points 已按时间排序）。"""
    if t <= points[0][0]:
        return float(points[0][1])
    if t >= points[-1][0]:
        return float(points[-1][1])
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            span = (t1 - t0).total_seconds()
            if span == 0:
                return float(v0)
            frac = (t - t0).total_seconds() / span
            return float(v0) + (float(v1) - float(v0)) * frac
    return float(points[-1][1])


class _Envelope:
    """连续潮位包络：分段上下界 + 全段最大涨落速率（厘米/小时）。"""

    def __init__(self, knots, rate_limit_cm_per_hour):
        self.knots = sorted((_instant(k['at']), float(k['min_cm']), float(k['max_cm'])) for k in knots)
        self.rate = float(rate_limit_cm_per_hour)
        if not self.knots:
            raise ValueError('channel envelope requires at least one knot')

    def band(self, t):
        lo = _interp([(k[0], k[1]) for k in self.knots], t)
        hi = _interp([(k[0], k[2]) for k in self.knots], t)
        return lo, hi

    def cone(self, pin_time, pin_level, t):
        """单个水尺 pin 通过速率约束投射到时刻 ``t`` 的潮位锥。"""
        hours = abs((t - pin_time).total_seconds()) / 3600.0
        delta = self.rate * hours
        return pin_level - delta, pin_level + delta


def crossing_window(departure_at, speed_kn, distance_nm=10.0):
    """由航速区间（节）给出经过浅滩的时间窗：最慢船速对应最晚到达、最宽暴露。

    返回 ``(窗口起, 窗口止, 快船速, 慢船速)``；窗口长度取最慢船速，保证对
    航速区间内的所有船速都覆盖浅滩暴露时刻。
    """
    departure = _instant(departure_at)
    slow = float(min(speed_kn))
    fast = float(max(speed_kn))
    return departure, departure + timedelta(hours=float(distance_nm) / slow), fast, slow


def assess_channel(channel, gauges, voyage, boarding):
    """对整条浅水通道做连续包络裁定。

    参数
    ----
    channel: 通道事件，含 ``envelope``（knots + rate_limit_cm_per_hour）、
        ``speed_kn``、可选 ``distance_nm`` 与 ``net_clearance_cm``。
    gauges: 全部水尺原件事件（函数内部自行过滤校准失效件）。
    voyage: 航次事件，提供 ``departure_at``、``required_level_cm`` 与 ``hull``。
    boarding: ``[{'weight_kg','lane','lever_arm_cm'}, ...]`` 实际登船清单，
        由 rules 层按最新 stow/restow 状态合成。

    返回
    ----
    dict，包含每个判断点的可行上下界、最危险时刻、最坏/最好曲线见证、
    水尺可用性与最终裁定（``safe`` / ``frozen`` / ``inconsistent``）。
    """
    good = valid_gauges(gauges)
    env = _Envelope(channel['envelope']['knots'], channel['envelope']['rate_limit_cm_per_hour'])

    distance = float(channel.get('distance_nm', 10.0))
    speeds = channel['speed_kn']
    window_start, window_end, fast, slow = crossing_window(voyage['departure_at'], speeds, distance)

    boarded_weight = sum(float(b['weight_kg']) for b in boarding)
    draft = hull_draft_cm(voyage, boarded_weight)
    clearance = float(channel.get('net_clearance_cm', 0))
    # 连续通道需求完全由实载吃水与净深决定；voyage.required_level_cm 只服务于
    # 离散潮窗布尔裁定 evaluate，不能当成整条水道的恒定潮位。
    required = required_level_for(draft, clearance)

    pins = sorted((_instant(g['observed_at']), float(g['level_cm']), g['gauge_id']) for g in good)

    def bounds_at(t):
        """时刻 t 上与全部原件一致的最紧可行水位区间。

        取包络分段带与每个水尺 pin 的速率锥的交集——这是连续约束，
        不是端点线性插值。
        """
        lo, hi = env.band(t)
        for pin_t, pin_level, _ in pins:
            cone_lo, cone_hi = env.cone(pin_t, pin_level, t)
            lo, hi = max(lo, cone_lo), min(hi, cone_hi)
            if lo > hi:
                return None
        return lo, hi

    # 水尺原件必须落在包络之内，否则原件与授权包络互相矛盾
    inconsistent = []
    for pin_t, pin_level, gauge_id in pins:
        band = bounds_at(pin_t)
        if band is None or not (band[0] - 1e-9 <= pin_level <= band[1] + 1e-9):
            inconsistent.append(gauge_id)

    # 分析上必须检查的关键时刻：包络结点、水尺时刻，再加分钟级密采样
    critical = {k[0] for k in env.knots if window_start <= k[0] <= window_end}
    critical |= {p[0] for p in pins if window_start <= p[0] <= window_end}
    steps = int((window_end - window_start).total_seconds() // 60)
    timeline = [window_start + timedelta(minutes=i) for i in range(steps + 1)]
    timeline += [t for t in critical if t not in set(timeline)]
    timeline.sort()

    point_evidence = []
    bands = []
    worst = None
    overall_lo, overall_hi = None, None
    for t in timeline:
        band = bounds_at(t)
        if band is None:
            # 该时刻不存在任何与全部原件一致的水位：原件与包络互相矛盾
            inconsistent.append('band@' + t.isoformat())
            bands.append((t, None))
            continue
        lo, hi = band
        bands.append((t, band))
        overall_lo = lo if overall_lo is None else min(overall_lo, lo)
        overall_hi = hi if overall_hi is None else max(overall_hi, hi)
        margin = lo - required
        record = {'at': t.isoformat(), 'min_feasible_cm': round(lo, 2),
                  'max_feasible_cm': round(hi, 2), 'required_cm': round(required, 2),
                  'min_margin_cm': round(margin, 2)}
        point_evidence.append(record)
        if worst is None or margin < worst['min_margin_cm']:
            worst = record

    # 曲线见证：构造两条显式满足速率约束的连续折线。逐点独立取锥界会产生
    # 相邻采样斜率超限的折线，因此做前向+后向两次速率扫掠取交集，得到与
    # 全部原件（含窗口两侧水尺 pin）一致的最坏/最好可行曲线。
    feasible_bands = [(t, b) for t, b in bands if b is not None]

    def _sweep(pick):
        n = len(feasible_bands)
        forward = [None] * n
        prev_t, prev_level = None, None
        for i, (t, (lo, hi)) in enumerate(feasible_bands):
            target = pick(lo, hi)
            if prev_t is None:
                level = target
            else:
                reach = env.rate * (t - prev_t).total_seconds() / 3600.0
                level = min(max(target, prev_level - reach), prev_level + reach)
            forward[i] = min(max(level, lo), hi)
            prev_t, prev_level = t, forward[i]
        backward = [None] * n
        prev_t, prev_level = None, None
        for i in range(n - 1, -1, -1):
            t, (lo, hi) = feasible_bands[i]
            target = pick(lo, hi)
            if prev_t is None:
                level = target
            else:
                reach = env.rate * (prev_t - t).total_seconds() / 3600.0
                level = min(max(target, prev_level - reach), prev_level + reach)
            backward[i] = min(max(level, lo), hi)
            prev_t, prev_level = t, backward[i]
        levels = []
        for f, b_, (t, (lo, hi)) in zip(forward, backward, feasible_bands):
            reachable_lo, reachable_hi = min(f, b_), max(f, b_)
            target = pick(lo, hi)
            levels.append(min(max(target, reachable_lo), reachable_hi))
        return levels

    def build_witness(pick):
        if not feasible_bands:
            return []
        return [{'at': t.isoformat(), 'level_cm': round(lvl, 2)}
                for (t, _), lvl in zip(feasible_bands, _sweep(pick))]

    low_witness = build_witness(lambda lo, hi: lo)
    high_witness = build_witness(lambda lo, hi: hi)
    if not feasible_bands:
        low_witness = [{'at': t.isoformat(), 'level_cm': round(env.band(t)[0], 2)} for t in timeline]
        high_witness = [{'at': t.isoformat(), 'level_cm': round(env.band(t)[1], 2)} for t in timeline]

    in_window_pins = [(pt, pl, gid) for pt, pl, gid in pins if window_start <= pt <= window_end]

    def honors(witness):
        if not feasible_bands:
            return False
        witness_times = {_instant(q['at']): q['level_cm'] for q in witness}
        # 只要求落在分析窗口内的水尺 pin 被见证精确命中
        for pin_t, pin_level, _ in in_window_pins:
            match = witness_times.get(pin_t)
            if match is None or abs(match - pin_level) > 0.5:
                return False
        for a, b in zip(witness, witness[1:]):
            hours = (_instant(b['at']) - _instant(a['at'])).total_seconds() / 3600.0
            # 见证水位已 round 到 0.01 cm，允许对应的舍入容差（每分钟 1 cm/h）
            if hours and abs(b['level_cm'] - a['level_cm']) / hours > env.rate + 1.0:
                return False
            lo, hi = env.band(_instant(a['at']))
            if not (lo - 0.02 <= a['level_cm'] <= hi + 0.02):
                return False
        return True

    if inconsistent:
        verdict = 'inconsistent'
    elif worst is None:
        verdict = 'frozen'
    elif worst['min_margin_cm'] + 1e-9 >= 0:
        verdict = 'safe'
    else:
        verdict = 'frozen'

    return {
        'voyage_id': channel['voyage_id'],
        'verdict': verdict,
        'draft_cm': round(draft, 2),
        'required_level_cm': round(required, 2),
        'boarded_weight_kg': boarded_weight,
        'window_start': window_start.isoformat(),
        'window_end': window_end.isoformat(),
        'speed_kn': [fast, slow],
        'gauges_used': [g['gauge_id'] for g in good],
        'gauges_rejected': [g.get('gauge_id') for g in gauges if g not in good],
        'inconsistent_gauges': sorted(set(inconsistent)),
        'overall_min_feasible_cm': None if overall_lo is None else round(overall_lo, 2),
        'overall_max_feasible_cm': None if overall_hi is None else round(overall_hi, 2),
        'most_dangerous': worst,
        'point_evidence': point_evidence,
        'witness_low_curve': low_witness,
        'witness_high_curve': high_witness,
        'witnesses_honor_envelope': honors(low_witness) and honors(high_witness),
        'load_version': boarding[0].get('load_version', 1) if boarding else 1,
    }


def curve_feasible(channel, gauges, curve_points):
    """判断一条给定真实曲线是否与包络和全部有效水尺原件一致。

    用于审计：两世界给出相同原件，但各自的真实曲线都必须先通过这个一致性
    检验，才能说明“同一输入下存在一条危险曲线”。
    """
    good = valid_gauges(gauges)
    env = _Envelope(channel['envelope']['knots'], channel['envelope']['rate_limit_cm_per_hour'])
    pts = sorted((_instant(p['at']), float(p['level_cm'])) for p in curve_points)
    for a, b in zip(pts, pts[1:]):
        hours = (b[0] - a[0]).total_seconds() / 3600.0
        if hours and abs(b[1] - a[1]) / hours > env.rate + 1e-9:
            return False
    for t, level in pts:
        lo, hi = env.band(t)
        if not (lo - 1e-9 <= level <= hi + 1e-9):
            return False
    for g in good:
        gt = _instant(g['observed_at'])
        interp_level = _interp(pts, gt)
        if abs(interp_level - float(g['level_cm'])) > 0.5:
            return False
    return True
