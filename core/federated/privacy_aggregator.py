#!/usr/bin/env python3
"""
火种系统 (FireSeed) 联邦学习安全聚合器
==========================================
提供差分隐私保护下的模型参数聚合：
- 拉普拉斯噪声机制 (ε-differential privacy)
- 更新裁剪（限制敏感度）
- 支持多客户端更新聚合
- 完全本地执行，不依赖外部服务
"""

import math
import random
from typing import List, Optional


class SecureAggregator:
    """
    安全聚合器：对客户端上传的模型更新进行裁剪并添加拉普拉斯噪声，
    保证 ε-差分隐私。

    使用方式：
        agg = SecureAggregator(epsilon=1.0, clip_norm=1.0)
        global_model = agg.aggregate(client_updates)
    """

    def __init__(self,
                 epsilon: float = 1.0,
                 clip_norm: float = 1.0,
                 seed: Optional[int] = None):
        """
        :param epsilon:   隐私预算 ε，越小隐私保护越强，噪声越大
        :param clip_norm: L2 裁剪阈值，超过此范数的更新将被裁剪
        :param seed:      随机种子（用于复现实验）
        """
        if epsilon <= 0:
            raise ValueError("epsilon 必须大于 0")
        self.epsilon = epsilon
        self.clip_norm = clip_norm
        self.rng = random.Random(seed)

    # ---------- 公共接口 ----------
    def aggregate(self, updates: List[List[float]]) -> List[float]:
        """
        对多个客户端更新进行安全聚合。
        步骤：
        1. 裁剪每个更新，使其 L2 范数不超过 clip_norm
        2. 求和所有裁剪后的更新
        3. 添加拉普拉斯噪声，满足 (ε, 0)-差分隐私
        4. 取平均并返回

        :param updates: 每个元素是一个列表，长度必须一致
        :return: 聚合后的参数更新列表
        """
        if not updates:
            raise ValueError("更新列表不能为空")

        dim = len(updates[0])
        for upd in updates:
            if len(upd) != dim:
                raise ValueError("所有更新维度必须一致")

        # 1. 裁剪
        clipped = [self._clip(upd) for upd in updates]

        # 2. 求和
        num_clients = len(clipped)
        summed = [0.0] * dim
        for i in range(dim):
            summed[i] = sum(upd[i] for upd in clipped)

        # 3. 添加拉普拉斯噪声
        scale = self.clip_norm / (self.epsilon * num_clients)  # 敏感度 / ε
        noisy_sum = [v + self._laplace_noise(scale) for v in summed]

        # 4. 平均
        result = [v / num_clients for v in noisy_sum]
        return result

    def aggregate_weighted(self,
                           updates: List[List[float]],
                           weights: Optional[List[float]] = None) -> List[float]:
        """
        加权版本的安全聚合，权重用于反映不同客户端的可信度或数据量。
        若未提供权重，则退化为等权平均。

        :param updates: 客户端更新列表
        :param weights: 对应权重列表（默认等权）
        """
        if not updates:
            raise ValueError("更新列表不能为空")

        dim = len(updates[0])
        num = len(updates)
        if weights is None:
            weights = [1.0] * num
        if len(weights) != num:
            raise ValueError("权重数量必须与更新数量一致")

        # 裁剪
        clipped = [self._clip(upd) for upd in updates]

        # 加权求和
        total_weight = sum(weights)
        if total_weight <= 0:
            raise ValueError("权重总和必须为正")

        summed = [0.0] * dim
        for i in range(num):
            w = weights[i]
            for d in range(dim):
                summed[d] += clipped[i][d] * w

        # 噪声
        scale = self.clip_norm / (self.epsilon * total_weight)
        noisy = [v + self._laplace_noise(scale) for v in summed]

        # 归一化
        return [v / total_weight for v in noisy]

    # ---------- 内部方法 ----------
    def _clip(self, vector: List[float]) -> List[float]:
        """对向量进行 L2 裁剪"""
        norm = math.sqrt(sum(v * v for v in vector))
        if norm <= self.clip_norm:
            return vector[:]   # 返回副本
        # 缩放至 clip_norm
        ratio = self.clip_norm / norm
        return [v * ratio for v in vector]

    def _laplace_noise(self, scale: float) -> float:
        """生成拉普拉斯噪声，参数 scale = b"""
        # 使用均匀分布逆变换：L(0,b)
        u = self.rng.uniform(-0.5, 0.5)
        return -scale * math.copysign(1.0, u) * math.log(1.0 - 2.0 * abs(u))

    # ---------- 辅助功能 ----------
    def set_privacy_budget(self, epsilon: float) -> None:
        """在线调整隐私预算"""
        if epsilon <= 0:
            raise ValueError("epsilon 必须大于 0")
        self.epsilon = epsilon

    def set_clip_norm(self, clip_norm: float) -> None:
        """在线调整裁剪阈值"""
        if clip_norm <= 0:
            raise ValueError("clip_norm 必须大于 0")
        self.clip_norm = clip_norm

    def get_noise_scale(self, num_clients: int = 1) -> float:
        """返回当前配置下的噪声规模 (敏感度 / ε)"""
        return self.clip_norm / (self.epsilon * max(1, num_clients))
