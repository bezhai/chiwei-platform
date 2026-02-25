"""
长期任务使用示例
"""

import asyncio

from app.long_tasks import (
    BaseTask,
    TaskStatus,
    get_task_status,
    submit_task,
    task_register,
)


@task_register("multi_step_calculation")
class MultiStepCalculation(BaseTask):
    """
    多步骤计算任务示例

    任务流程：
    1. 初始化：接收 start 参数
    2. 第一步：计算平方
    3. 第二步：加倍
    4. 完成
    """

    async def execute(self):
        step = self.result.get("step", 0)

        if step == 0:
            # 第一步：计算平方
            value = self.result.get("start", 0)
            new_value = value**2
            return {"step": 1, "value": new_value}, TaskStatus.COMMIT

        elif step == 1:
            # 第二步：加倍
            value = self.result["value"]
            final_value = value * 2
            return {"step": 2, "value": final_value}, TaskStatus.DONE

        else:
            raise ValueError("Invalid step")


async def main():
    # 提交任务
    task_id = await submit_task(
        task_type="multi_step_calculation", initial_result={"start": 10}
    )
    print(f"Task submitted: {task_id}")

    # 查询状态
    while True:
        status = await get_task_status(task_id)
        print(f"Status: {status['status']}, Result: {status['current_result']}")

        if status["status"] in [TaskStatus.DONE, TaskStatus.FAILED]:
            break

        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
