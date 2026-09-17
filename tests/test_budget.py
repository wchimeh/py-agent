# @File:     test_budget.py
# @DateTime: 2026/09/17
"""BudgetPool：进程级 token 预算总闸（主循环与全部子循环共享）。"""
from concurrent.futures import ThreadPoolExecutor

from agent.budget import BudgetPool


def test_pool_consume_within_total():
    pool = BudgetPool(1000)
    assert pool.consume(100) is True
    assert pool.used == 100 and pool.remaining == 900


def test_pool_rejects_overdraft_without_deduction():
    pool = BudgetPool(100)
    assert pool.consume(80) is True
    assert pool.consume(30) is False        # 欠足拒付
    assert pool.used == 80                  # 拒付不扣减


def test_pool_concurrent_deduction_consistent():
    # 350 总量、8 线程各 consume(50)：恰好 7 成功 1 拒付，used==350
    pool = BudgetPool(350)
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: pool.consume(50), range(8)))
    assert results.count(True) == 7 and results.count(False) == 1
    assert pool.used == 350
