import { apiGetCommand, apiGetTask } from './health'
import { ERROR_MESSAGES } from '../types/api'
import type { CommandItem, TaskItem } from '../types/health'

async function waitForOperation<T extends { id: string; state: string; error?: string | null }>(
  task: T, read: (id: string) => Promise<T>, signal?: AbortSignal, timeoutMs = 60000,
): Promise<T> {
  const deadline = Date.now() + timeoutMs
  while (true) {
    if (signal?.aborted) throw new Error('操作窗口已关闭。')
    if (task.state === 'succeeded') return task
    if (task.state === 'failed') throw new Error(ERROR_MESSAGES[task.error || 'operation_failed'] || '后台任务失败。')
    if (task.state === 'unknown') throw new Error('命令结果未知，请刷新运行状态，不要重复启动。')
    if (Date.now() >= deadline) throw new Error('后台任务尚未完成，请在活动记录中查看结果。')
    task = await read(task.id)
    if (!['succeeded', 'failed', 'unknown'].includes(task.state)) {
      await new Promise<void>((resolve) => setTimeout(resolve, 1000))
    }
  }
}

export function waitForTask<Result>(task: TaskItem<Result>, signal?: AbortSignal,
                                    timeoutMs?: number): Promise<TaskItem<Result>> {
  return waitForOperation(task, apiGetTask<Result>, signal, timeoutMs)
}

export function waitForCommand(command: CommandItem, signal?: AbortSignal): Promise<CommandItem> {
  return waitForOperation(command, apiGetCommand, signal)
}
