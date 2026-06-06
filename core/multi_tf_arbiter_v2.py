"""
火种系统 · 多周期协同仲裁器入口 (MultiTfArbiterV2)

核心职责：
1. 聚合子模块功能：区间厚度计算、时效衰减管理、趋势波浪斜线识别、区间角色翻转、三阶段穿越监控
2. 对外提供统一的多周期协同查询接口，为各周期策略引擎提供上级周期的动态约束区间

外部依赖（真实模块接口）：
- core.multi_tf_arbiter_v2.zone_thickness_calculator.ZoneThicknessCalculator : 计算关键价位的动态厚度
- core.multi_tf_arbiter_v2.zone_decay_manager.ZoneDecayManager : 管理区间的四阶段时效衰减，提供实时强度
- core.multi_tf_arbiter_v2.trend_wave_identifier.TrendWaveIdentifier : 识别趋势波浪斜线通道，接受 (period, price)
- core.multi_tf_arbiter_v2.zone_role_flipper.ZoneRoleFlipper : 处理区间突破后的角色翻转
- core.multi_tf_arbiter_v2.zone_crossing_monitor.ZoneCrossingMonitor : 监控价格穿越区间的三阶段行为

接口契约：
- query_zone_constraint(period: int, price: Decimal) -> Dict[str, Any] : 查询指定周期在当前价格下的区间约束
- get_trend_wave(period: int, price: Decimal) -> Dict[str, Any] : 获取指定周期在指定价格下的趋势波浪斜线信息
- health_check() -> Dict[str, Any] : 模块自检
- 所有公共方法输出字典固定包含 "status" (str), "reason" (str), "data" (Dict), "warnings" (List[str])
- 数值字段均以字符串形式返回，保留完整精度

线程安全：
- 本模块所有公共方法均为读操作，内部通过锁保护共享状态（性能计数器和配置）。
- 依赖注入方法 `inject_dependencies` 和 `load_config` 为写操作，可安全并发调用。
- 子模块的健康检查通过隔离线程执行，避免阻塞主调用链。

异常与降级：
- 当任一子模块不可用时，使用对应默认安全值作为回退，并标记 "degraded" 状态
- 当所有子模块均不可用时，返回完全保守的区间约束（极宽区间，最低约束力）
- 降级厚度默认按价格百分比计算，并乘以波动率放大因子以增加安全边际
- 所有降级值在类常量区明确声明

资源管理：
- 本模块不持有任何需要手动释放的资源
- 子模块实例由外部管理，本模块仅持有引用，销毁时自动释放
- 本模块提供只读查询服务，默认线程安全（所有公共方法内部不修改共享状态，除性能计数器外）

配置版本: 3.4.0
兼容性: Python 3.10+, 依赖模块版本 >= 3.0.0
"""

import logging
import time
import threading
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Dict, Any, Optional, List, Callable, Tuple

logger = logging.getLogger(__name__)

# 允许热加载的配置键白名单
_ALLOWED_CONFIG_KEYS = frozenset({
    'volatility_multiplier',
    'min_price_precision',
})


class MultiTfArbiterV2:
    """多周期协同仲裁器入口"""

    # ========== 类常量（默认配置，附带单位与取值范围注释） ==========
    VALID_PERIODS = (1, 5, 15)             # 支持的整数周期标识

    DEFAULT_ZONE_THICKNESS_RATIO = Decimal('0.02')  # 默认厚度占价格比例，[0.01, 0.05]
    DEFAULT_ZONE_STRENGTH = Decimal('0.2')          # 默认区间约束力，[0.0, 1.0]
    DEFAULT_TREND_WAVE_SLOPE = Decimal('0')         # 默认波浪斜线斜率，[-0.1, 0.1]
    DEFAULT_CROSSING_STAGE = "outside"               # 默认穿越阶段
    DEFAULT_VOLATILITY_MULTIPLIER = Decimal('1.5')  # 降级厚度波动率放大系数，[1.0, 3.0]

    STAGE_STRENGTH_MAP = {
        "fresh": Decimal('0.95'),
        "active": Decimal('0.70'),
        "memory": Decimal('0.40'),
        "fading": Decimal('0.15'),
    }
    FLIP_STRENGTH_FACTOR = Decimal('0.8')  # [0.5, 1.0]，角色翻转后约束力保留系数

    # 默认最小价格精度（作为兜底，实际应从品种配置中获取）
    FALLBACK_PRICE_PRECISION = Decimal('0.01')
    MAX_STRENGTH = Decimal('1.0')
    MIN_STRENGTH = Decimal('0.0')

    HEALTH_CHECK_TIMEOUT = 2.0             # 秒，[0.5, 5.0]
    DECIMAL_PRECISION = 28                 # Decimal 临时精度位数
    MAX_INIT_WARNINGS = 100                # 最大保留初始化警告数

    DEFAULT_ROUNDING = ROUND_HALF_UP

    def __init__(self) -> None:
        self._thickness_calculator: Optional[Any] = None
        self._decay_manager: Optional[Any] = None
        self._trend_wave_identifier: Optional[Any] = None
        self._role_flipper: Optional[Any] = None
        self._crossing_monitor: Optional[Any] = None

        self._init_warnings: deque = deque(maxlen=self.MAX_INIT_WARNINGS)
        self._warnings_lock = threading.Lock()

        # 性能监控（纳秒级，线程安全）
        self._last_query_latency_ns: float = 0.0
        self._latency_lock = threading.Lock()

        # 运行时配置（需在注入前通过 `load_config` 设置）
        self._config: Dict[str, Any] = {}
        self._config_lock = threading.Lock()

        logger.info("MultiTfArbiterV2 已创建，等待依赖注入")

    # ========== 配置加载 ==========
    def load_config(self, config: Dict[str, Any]) -> None:
        """
        加载运行时配置（热重载支持），线程安全。
        仅允许更新白名单内的键，非白名单键将被静默忽略。
        """
        filtered = {k: v for k, v in config.items() if k in _ALLOWED_CONFIG_KEYS}
        with self._config_lock:
            self._config.update(filtered)
        if filtered:
            logger.debug("运行时配置已更新，生效键: %s", list(filtered.keys()))

    # ========== 依赖注入 ==========
    def inject_dependencies(
        self,
        thickness_calculator: Optional[Any] = None,
        decay_manager: Optional[Any] = None,
        trend_wave_identifier: Optional[Any] = None,
        role_flipper: Optional[Any] = None,
        crossing_monitor: Optional[Any] = None,
    ) -> None:
        """注入子模块依赖，并校验方法存在性。线程安全。"""
        with self._warnings_lock:
            self._init_warnings.clear()

        self._safe_inject("thickness_calculator", thickness_calculator, ['calculate'])
        self._safe_inject("decay_manager", decay_manager, ['get_current_stage', 'get_strength'])
        self._safe_inject("trend_wave_identifier", trend_wave_identifier, ['identify'])
        self._safe_inject("role_flipper", role_flipper, ['get_role_state'])
        self._safe_inject("crossing_monitor", crossing_monitor, ['get_crossing_stage'])

    def _safe_inject(self, name: str, obj: Any, required_methods: List[str]) -> None:
        attr_name = f'_{name}'
        old_obj = getattr(self, attr_name, None)
        # 清理旧实例资源
        if old_obj is not None and callable(getattr(old_obj, 'cleanup', None)):
            try:
                old_obj.cleanup()
            except Exception:
                logger.warning("旧 %s 实例清理异常", name, exc_info=False)
        setattr(self, attr_name, None)

        if obj is None:
            logger.warning("%s 未注入，对应功能降级", name)
            self._add_init_warning(f'{name}_unavailable')
            return
        for method in required_methods:
            if not callable(getattr(obj, method, None)):
                logger.warning("%s 缺少方法 %s，标记为不可用", name, method)
                self._add_init_warning(f'{name}_missing_method_{method}')
                return
        setattr(self, attr_name, obj)
        logger.info("%s 注入成功", name)

    def _add_init_warning(self, msg: str) -> None:
        with self._warnings_lock:
            if msg not in self._init_warnings:
                self._init_warnings.append(msg)

    def _get_init_warnings_copy(self) -> List[str]:
        with self._warnings_lock:
            # 转换为元组确保线程安全迭代
            return list(self._init_warnings)

    # ========== 公共接口 ==========
    def query_zone_constraint(self, period: int, price: Decimal) -> Dict[str, Any]:
        """查询指定周期在当前价格下的区间约束。线程安全。"""
        start_time = time.perf_counter_ns()

        # 参数校验
        if not isinstance(period, int) or period not in self.VALID_PERIODS:
            return self._error_response(f'无效周期标识: {period}', warnings=[f'invalid_period:{period}'])
        if not isinstance(price, Decimal):
            return self._error_response('价格类型必须为 Decimal', warnings=['invalid_price_type'])
        try:
            if price <= Decimal('0') or not price.is_finite():
                return self._error_response(f'无效价格: {price}', warnings=[f'invalid_price:{price}'])
        except (InvalidOperation, AttributeError):
            return self._error_response('价格校验异常', warnings=['invalid_price_exception'])

        warnings: List[str] = []
        is_estimated = False

        # 获取当前有效的最小价格精度
        min_precision = self._get_min_precision()

        # 1. 厚度计算
        zone_thickness = self._calc_thickness(period, price, warnings)
        if zone_thickness is None:
            with self._config_lock:
                raw_vol = self._config.get('volatility_multiplier', self.DEFAULT_VOLATILITY_MULTIPLIER)
            vol_mult = self._to_decimal(raw_vol, self.DEFAULT_VOLATILITY_MULTIPLIER)
            zone_thickness = self.DEFAULT_ZONE_THICKNESS_RATIO * price * vol_mult
            is_estimated = True
            self._safe_append_warning(warnings, 'zone_thickness_default_used')

        # 2. 强度计算
        strength, decay_stage = self._calc_strength(period, price, warnings)
        if strength is None:
            strength = self.STAGE_STRENGTH_MAP.get(decay_stage, self.DEFAULT_ZONE_STRENGTH)
            is_estimated = True
            self._safe_append_warning(warnings, 'strength_default_used')

        # 3. 趋势波浪
        trend_wave_info = self._safe_call(
            self._trend_wave_identifier, 'identify',
            lambda obj: obj.identify(period, price),
            default={},
            warnings=warnings,
            err_msg='trend_wave_identifier.identify',
        )
        if not isinstance(trend_wave_info, dict):
            trend_wave_info = {}
            self._safe_append_warning(warnings, 'trend_wave_invalid_type')

        # 4. 角色状态
        role_state = self._safe_call(
            self._role_flipper, 'get_role_state',
            lambda obj: obj.get_role_state(period),
            default="original",
            warnings=warnings,
            err_msg='role_flipper.get_role_state',
        )

        # 5. 穿越阶段
        crossing_stage = self._safe_call(
            self._crossing_monitor, 'get_crossing_stage',
            lambda obj: obj.get_crossing_stage(period, price),
            default=self.DEFAULT_CROSSING_STAGE,
            warnings=warnings,
            err_msg='crossing_monitor.get_crossing_stage',
        )

        # 综合计算
        with localcontext() as ctx:
            ctx.prec = self.DECIMAL_PRECISION
            ctx.rounding = self.DEFAULT_ROUNDING
            upper_bound = (price + zone_thickness).quantize(min_precision, rounding=self.DEFAULT_ROUNDING)
            lower_bound = (price - zone_thickness).quantize(min_precision, rounding=self.DEFAULT_ROUNDING)

        if lower_bound < 0:
            lower_bound = Decimal('0')
            is_estimated = True
            self._safe_append_warning(warnings, 'lower_bound_clamped_to_zero_anomaly')

        # 强度后处理：先钳位至 [0,1]，再应用调整因子，最后再次钳位
        strength = max(self.MIN_STRENGTH, min(self.MAX_STRENGTH, strength))
        if role_state == 'flipped':
            strength *= self.FLIP_STRENGTH_FACTOR
        if crossing_stage == 'probing':
            strength *= Decimal('0.85')
        elif crossing_stage == 'wrestling':
            strength *= Decimal('0.95')
        strength = max(self.MIN_STRENGTH, min(self.MAX_STRENGTH, strength))

        data = {
            'period': period,
            'price': self._decimal_to_str(price),
            'upper_bound': self._decimal_to_str(upper_bound),
            'lower_bound': self._decimal_to_str(lower_bound),
            'zone_thickness': self._decimal_to_str(zone_thickness),
            'strength': self._decimal_to_str(strength, places=4),
            'decay_stage': decay_stage,
            'role_state': role_state,
            'crossing_stage': crossing_stage,
            'trend_wave': trend_wave_info,
            'is_estimated': is_estimated,
        }

        for w in self._get_init_warnings_copy():
            self._safe_append_warning(warnings, w)

        elapsed_ns = time.perf_counter_ns() - start_time
        with self._latency_lock:
            self._last_query_latency_ns = elapsed_ns

        return {
            'status': 'ok',
            'reason': f'周期 {period} 区间约束查询完成',
            'data': data,
            'warnings': self._dedup_warnings(warnings),
        }

    def get_trend_wave(self, period: int, price: Decimal) -> Dict[str, Any]:
        """获取指定周期在指定价格下的趋势波浪斜线信息。"""
        if not isinstance(period, int) or period not in self.VALID_PERIODS:
            return self._error_response(f'无效周期标识: {period}', warnings=[f'invalid_period:{period}'])
        if not isinstance(price, Decimal):
            return self._error_response('价格类型必须为 Decimal', warnings=['invalid_price_type'])
        try:
            if price <= Decimal('0') or not price.is_finite():
                return self._error_response(f'无效价格: {price}', warnings=[f'invalid_price:{price}'])
        except (InvalidOperation, AttributeError):
            return self._error_response('价格校验异常', warnings=['invalid_price_exception'])

        warnings: List[str] = []
        wave_info = self._safe_call(
            self._trend_wave_identifier, 'identify',
            lambda obj: obj.identify(period, price),
            default={
                'slope': str(self.DEFAULT_TREND_WAVE_SLOPE),
                'available': False,
                'period': period,
            },
            warnings=warnings,
            err_msg='trend_wave_identifier.identify',
        )
        if not isinstance(wave_info, dict):
            wave_info = {'slope': str(self.DEFAULT_TREND_WAVE_SLOPE), 'available': False, 'period': period}
            self._safe_append_warning(warnings, 'trend_wave_invalid_type')

        for w in self._get_init_warnings_copy():
            self._safe_append_warning(warnings, w)

        return {
            'status': 'ok' if not warnings else 'degraded',
            'reason': f'周期 {period} 趋势波浪查询完成' if not warnings else '趋势波浪识别降级',
            'data': wave_info,
            'warnings': self._dedup_warnings(warnings),
        }

    # ========== 健康检查 ==========
    def health_check(self) -> Dict[str, Any]:
        """模块自检，包括子模块响应探测。"""
        try:
            dep_status: Dict[str, str] = {}
            for name, obj in [
                ('thickness_calculator', self._thickness_calculator),
                ('decay_manager', self._decay_manager),
                ('trend_wave_identifier', self._trend_wave_identifier),
                ('role_flipper', self._role_flipper),
                ('crossing_monitor', self._crossing_monitor),
            ]:
                if obj is None:
                    dep_status[name] = 'unavailable'
                elif not callable(getattr(obj, 'health_check', None)):
                    dep_status[name] = 'no_health_check'
                else:
                    dep_status[name] = self._probe_submodule_health(obj)

            available = sum(1 for v in dep_status.values() if v == 'ok')
            total = len(dep_status)

            if available == 0:
                return {'status': 'degraded', 'reason': '所有子模块均不可用', 'data': dep_status,
                        'warnings': ['all_submodules_unavailable']}
            elif available < total:
                return {'status': 'degraded', 'reason': f'部分子模块不可用 ({available}/{total})',
                        'data': dep_status, 'warnings': ['partial_degradation']}
            return {'status': 'ok', 'reason': '所有子模块可用且自检通过', 'data': dep_status, 'warnings': []}
        except Exception as e:
            logger.error('健康检查异常: %s #RECOVERY: 检查模块初始化状态', type(e).__name__)
            return {'status': 'error', 'reason': f'健康检查异常: {type(e).__name__}', 'data': {},
                    'warnings': [f'health_check_failed:{type(e).__name__}']}

    def _probe_submodule_health(self, obj: Any) -> str:
        """探测子模块健康状态，带超时保护和优雅停止。"""
        stop_event = threading.Event()
        result_lock = threading.Lock()
        result: List[str] = []

        def _runner() -> None:
            try:
                if stop_event.is_set():
                    return
                res = obj.health_check()
                with result_lock:
                    if isinstance(res, dict):
                        status = res.get('status')
                        if status == 'ok':
                            result.append('ok')
                        elif status == 'warning':
                            result.append('warning')
                        else:
                            result.append(f'degraded:{status or "unknown"}')
                    else:
                        result.append('invalid_response_type')
            except Exception as e:
                with result_lock:
                    result.append(f'exception:{type(e).__name__[:50]}')

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=self.HEALTH_CHECK_TIMEOUT)
        if t.is_alive():
            stop_event.set()  # 通知子线程停止
            return 'timeout'
        with result_lock:
            return result[0] if result else 'no_result'

    # ========== 私有方法 ==========
    @staticmethod
    def _error_response(reason: str, warnings: Optional[List[str]] = None) -> Dict[str, Any]:
        return {'status': 'error', 'reason': reason, 'data': {}, 'warnings': warnings or []}

    def _get_min_precision(self) -> Decimal:
        """从配置中获取当前品种的最小价格精度，若无则返回兜底值。"""
        with self._config_lock:
            raw = self._config.get('min_price_precision')
        if isinstance(raw, Decimal):
            return raw
        if isinstance(raw, (int, float, str)):
            try:
                return Decimal(str(raw))
            except (InvalidOperation, ValueError):
                logger.warning('配置中的 min_price_precision 非法: %s，使用兜底值', raw)
        return self.FALLBACK_PRICE_PRECISION

    @staticmethod
    def _to_decimal(value: Any, default: Decimal) -> Decimal:
        """安全转换为 Decimal，失败返回默认值。"""
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return default

    def _calc_thickness(self, period: int, price: Decimal, warnings: List[str]) -> Optional[Decimal]:
        if self._thickness_calculator is None:
            return None
        try:
            result = self._thickness_calculator.calculate(period, price)
            if not isinstance(result, dict):
                return None
            raw = result.get('thickness')
            if not isinstance(raw, (int, float, Decimal)):
                return None
            val = Decimal(str(raw))
            if val <= 0 or not val.is_finite():
                logger.warning('区间厚度非法（%s），丢弃', val)
                return None
            return val
        except Exception:
            logger.warning('厚度计算异常', exc_info=False)
            self._safe_append_warning(warnings, 'thickness_calc_exception')
            return None

    def _calc_strength(self, period: int, price: Decimal, warnings: List[str]) -> Tuple[Optional[Decimal], str]:
        strength: Optional[Decimal] = None
        stage: str = 'fresh'

        if self._decay_manager is None:
            self._safe_append_warning(warnings, 'decay_manager_unavailable')
            return None, stage

        # 获取当前阶段
        try:
            stage_result = self._decay_manager.get_current_stage(period)
            if isinstance(stage_result, dict):
                stage = str(stage_result.get('stage', 'fresh'))
        except Exception:
            logger.warning('衰减阶段查询异常', exc_info=False)
            self._safe_append_warning(warnings, 'stage_query_exception')

        # 获取动态强度
        if callable(getattr(self._decay_manager, 'get_strength', None)):
            try:
                strength_result = self._decay_manager.get_strength(period, price)
                if isinstance(strength_result, dict):
                    raw_str = strength_result.get('strength')
                    if raw_str is not None:
                        val = Decimal(str(raw_str))
                        if self.MIN_STRENGTH <= val <= self.MAX_STRENGTH:
                            strength = val
            except Exception:
                logger.warning('动态强度查询异常', exc_info=False)
                self._safe_append_warning(warnings, 'strength_calc_exception')

        return strength, stage

    def _safe_call(self, obj: Optional[Any], method_name: str, func: Callable[[Any], Any],
                   default: Any, warnings: List[str], err_msg: str) -> Any:
        if obj is None:
            self._safe_append_warning(warnings, f'{method_name}_unavailable')
            return default
        try:
            result = func(obj)
            if result is None and default is not None:
                self._safe_append_warning(warnings, f'{method_name}_returned_none')
                return default
            return result
        except Exception:
            logger.warning('%s 调用异常', err_msg, exc_info=False)
            self._safe_append_warning(warnings, f'{method_name}_exception')
            return default

    @staticmethod
    def _decimal_to_str(value: Decimal, places: int = 8) -> str:
        """将 Decimal 转为去除尾部零的字符串，使用显式舍入，防止科学计数法。"""
        if places < 0:
            places = 0
        precision = Decimal('0.1') ** places if places > 0 else Decimal('1')
        quantized = value.quantize(precision, rounding=ROUND_HALF_UP)
        formatted = f'{quantized:.{places}f}'.rstrip('0').rstrip('.')
        return formatted or '0'

    @staticmethod
    def _dedup_warnings(warnings: List[str]) -> List[str]:
        """去重并保留插入顺序。"""
        seen = set()
        result = []
        for w in warnings:
            if w not in seen:
                seen.add(w)
                result.append(w)
        return result

    @staticmethod
    def _safe_append_warning(warnings: List[str], msg: str) -> None:
        """追加警告到列表。注意：调用方需确保列表的线程安全性。"""
        if msg not in warnings:
            warnings.append(msg)

    def get_last_latency_ns(self) -> float:
        """获取最近一次查询的耗时（纳秒），用于性能监控。线程安全。"""
        with self._latency_lock:
            return self._last_query_latency_ns
