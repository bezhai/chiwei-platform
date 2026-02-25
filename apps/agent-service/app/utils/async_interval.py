import asyncio


class AsyncIntervalChecker:
    def __init__(self, interval_seconds):
        """
        初始化异步间隔检查器

        Args:
            interval_seconds: 间隔秒数
        """
        self.interval_seconds = interval_seconds
        self.last_trigger_time = None

    def check(self):
        """
        检查是否达到间隔时间

        Returns:
            bool: 如果达到间隔时间返回True并更新触发时间，否则返回False
        """
        current_time = asyncio.get_event_loop().time()

        # 如果是第一次调用
        if self.last_trigger_time is None:
            self.last_trigger_time = current_time
            return True

        # 计算时间差
        time_diff = current_time - self.last_trigger_time

        # 如果时间差大于等于设定的间隔秒数
        if time_diff >= self.interval_seconds:
            self.last_trigger_time = current_time  # 更新触发时间
            return True
        else:
            return False
