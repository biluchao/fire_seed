#!/usr/bin/env python3
"""
火种系统 (FireSeed) 每日任务调度器
====================================
在每日凌晨 3:00 自动执行一系列维护任务：
- 因子 PSI 稳定性扫描与权重冻结建议
- 遗传算法种群多样性维护
- 失败基因库归档与蒸馏
- 操作审计日志压缩转储
- 权重长期基线更新
- 混沌测试报告生成
- 议会日报生成与推送
- 冷数据归档触发
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

from config.loader import ConfigLoader

logger = logging.getLogger("fire_seed.daily_tasks")


class DailyTaskScheduler:
    """每日凌晨执行的系统维护任务调度器。"""

    def __init__(self, config: ConfigLoader, engine: Optional["FireSeedEngine"] = None):
        self.config = config
        self.engine = engine          # 引擎引用，用于访问各个子系统
        self._last_run_date: Optional[str] = None

    async def run(self) -> None:
        """执行所有每日维护任务。应当由引擎在 03:00 附近调用。"""
        today = datetime.now().strftime("%Y%m%d")
        if self._last_run_date == today:
            logger.info("今日任务已执行，跳过")
            return

        logger.info("========== 开始执行每日任务 ==========")
        start = time.monotonic()

        # 依次执行，单个失败不影响后续
        await self._safe_exec("PSI 稳定性扫描", self._psi_stability_scan)
        await self._safe_exec("种群多样性维护", self._population_maintenance)
        await self._safe_exec("失败基因库归档", self._archive_failure_genes)
        await self._safe_exec("审计日志压缩转储", self._archive_audit_logs)
        await self._safe_exec("权重长期基线更新", self._update_long_term_baseline)
        await self._safe_exec("混沌测试报告", self._chaos_test_report)
        await self._safe_exec("议会日报推送", self._push_daily_council_report)
        await self._safe_exec("冷数据归档检查", self._trigger_cold_archive)
        await self._safe_exec("常规清理", self._general_cleanup)

        elapsed = time.monotonic() - start
        self._last_run_date = today
        logger.info(f"========== 每日任务完成，耗时 {elapsed:.1f} 秒 ==========")

    # ---------------------------------------------------------------
    async def _safe_exec(self, name: str, coro_func) -> None:
        """带异常保护的执行包装"""
        try:
            await coro_func()
        except Exception as e:
            logger.error(f"任务 [{name}] 执行失败: {e}", exc_info=True)

    # ======================== 各项任务实现 ========================
    async def _psi_stability_scan(self) -> None:
        """
        扫描所有活跃因子的 PSI 值，
        若 PSI > 0.25 则记录并建议冻结该因子。
        """
        if not self.engine:
            return
        try:
            weight_engine = self.engine.weight_engine
            # 假设权重引擎有方法获取所有因子的 PSI 数据
            # 若无，则从行为日志或监控模块获取
            if hasattr(weight_engine, 'get_all_psis'):
                psi_data = weight_engine.get_all_psis()
            else:
                # 模拟：无数据时跳过
                logger.info("PSI 数据不可用，跳过扫描。")
                return

            for factor, psi in psi_data.items():
                if psi > 0.25:
                    logger.warning(f"因子 [{factor}] PSI={psi:.3f} 超过阈值，建议冻结。")
                    # 写入行为日志
                    if self.engine.behavior_log:
                        self.engine.behavior_log.log(
                            EventType.SYSTEM, "DailyTask",
                            f"PSI 预警: {factor} = {psi:.3f}"
                        )
        except Exception as e:
            logger.error(f"PSI 扫描异常: {e}")

    async def _population_maintenance(self) -> None:
        """维护遗传算法种群的多样性，必要时注入移民个体。"""
        if not self.engine or not hasattr(self.engine, 'plugin_mgr'):
            return
        # 假设插件管理器中有进化模块的引用
        try:
            evo = getattr(self.engine, 'evo_factory', None)
            if evo and hasattr(evo, 'maintain_diversity'):
                evo.maintain_diversity()
                logger.info("种群多样性维护完成。")
        except Exception as e:
            logger.warning(f"种群维护未执行: {e}")

    async def _archive_failure_genes(self) -> None:
        """将失败基因库中的记录进行归档（压缩并写入冷存储），同时清理过期记录。"""
        if not self.engine:
            return
        try:
            # 假设失败基因库模块存在
            if hasattr(self.engine, 'failure_gene_db'):
                await self.engine.failure_gene_db.archive_old_entries(days=30)
        except Exception as e:
            logger.warning(f"失败基因归档未执行: {e}")

    async def _archive_audit_logs(self) -> None:
        """压缩并转存操作审计日志（例如超过 30 天的日志移至冷存储）。"""
        # 由 cold_archiver 或行为日志模块处理，这里只做触发或本地压缩
        try:
            if self.engine and hasattr(self.engine, 'cold_archiver'):
                await self.engine.cold_archiver.archive_expired()
                logger.info("审计日志归档已触发。")
        except Exception as e:
            logger.warning(f"审计日志归档失败: {e}")

    async def _update_long_term_baseline(self) -> None:
        """更新条件权重引擎中的长期中性基准（用于市场突变时的回退）。"""
        if not self.engine:
            return
        try:
            weight_engine = self.engine.weight_engine
            if hasattr(weight_engine, 'update_baseline'):
                weight_engine.update_baseline()
                logger.info("权重长期基准已更新。")
        except Exception as e:
            logger.warning(f"长期基准更新失败: {e}")

    async def _chaos_test_report(self) -> None:
        """触发混沌测试并生成报告（通常由外部脚本定时执行，这里仅记录）。"""
        # 混沌测试可能由独立的 cron 任务运行，这里只记录本次调度
        logger.info("混沌测试报告请求已记录（由独立脚本执行）。")

    async def _push_daily_council_report(self) -> None:
        """调用叙事官智能体生成并推送议会日报。"""
        if not self.engine:
            return
        try:
            if hasattr(self.engine, 'agent_council') and hasattr(self.engine.agent_council, 'generate_daily_report'):
                report = await self.engine.agent_council.generate_daily_report()
                # 推送至消息渠道
                if self.engine.notifier:
                    await self.engine.notifier.send_daily_report(report)
                logger.info("议会日报已生成并推送。")
        except Exception as e:
            logger.warning(f"日报推送失败: {e}")

    async def _trigger_cold_archive(self) -> None:
        """确保冷归档服务正常运行（若未启动则触发一次归档）。"""
        if self.engine and hasattr(self.engine, 'cold_archiver'):
            try:
                # 冷归档已在后台循环中运行，这里再主动触发一次
                await self.engine.cold_archiver.archive_expired()
            except Exception as e:
                logger.warning(f"冷归档触发失败: {e}")

    async def _general_cleanup(self) -> None:
        """清理临时文件、重置过期标识等。"""
        try:
            # 清理过期的缓存文件（如旧版本权重备份）
            pass
        except Exception as e:
            logger.warning(f"常规清理异常: {e}")
