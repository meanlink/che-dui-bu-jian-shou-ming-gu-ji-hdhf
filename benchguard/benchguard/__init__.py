"""行业对标发布管控系统（benchguard）。

零第三方依赖：仅使用 Python 标准库（http.server / sqlite3 / threading）。
核心规则：冻结批次 + 入选组合分组 + k=5 匿名门槛的对标发布管控。
"""

__version__ = "1.0.0"
